"""Versioned, opt-in Chat Completions capabilities; no network or credentials."""

import hashlib
import json
from copy import deepcopy

from .model_parameters import merge_parameters
from .security import ApiError

PRESETS = {
    "deepseek-chat-v1": {
        "id": "deepseek-chat-v1",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "defaults": {"thinking": {"type": "disabled"}},
        "formats": ["text", "json_object"],
        "token_field": "max_tokens",
        "notes": "默认关闭思考；Chat 不支持 json_schema，由 Core 降级 JSON Output 并继续校验业务结构。",
        "docs_url": "https://api-docs.deepseek.com/api/create-chat-completion/",
    },
    "qwen-chat-v1": {
        "id": "qwen-chat-v1",
        "label": "千问（百炼 · 北京）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-flash"],
        "defaults": {"thinking": {"type": "disabled"}},
        "formats": ["text", "json_object"],
        "token_field": "max_tokens",
        "notes": "思考开关转为 enable_thinking；本批型号的 JSON Output 仅用于非思考模式。地址地域需与 Key 一致。",
        "docs_url": "https://help.aliyun.com/zh/model-studio/qwen-structured-output",
    },
    "openai-gpt41-v1": {
        "id": "openai-gpt41-v1",
        "label": "OpenAI / ChatGPT 系列（API）",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4.1", "gpt-4.1-mini"],
        "defaults": {},
        "formats": ["text", "json_object", "json_schema"],
        "token_field": "max_completion_tokens",
        "notes": "GPT-4.1 系列非推理模型；保留严格 Schema，输出 Token 转为 max_completion_tokens。需 OpenAI API Key。",
        "docs_url": "https://developers.openai.com/api/docs/models/gpt-4.1",
    },
}


def catalog():
    return [
        {**deepcopy(preset), "parameter_rules": parameter_rules(preset["id"]),
         "protocol": "chat_completions", "reviewed_at": "2026-09-24"} for preset in PRESETS.values()
    ]


def parameter_rules(preset_id):
    """UI constraints, not vendor identifiers or authorization policy."""
    if preset_id == "deepseek-chat-v1":
        return {"reasoningEfforts": ["low", "high"], "thinkingEnabled": True, "frequencyPenalty": True}
    if preset_id == "qwen-chat-v1":
        return {"reasoningEfforts": [], "thinkingEnabled": False, "frequencyPenalty": False,
                "temperatureExclusiveMax": 2}
    if preset_id == "openai-gpt41-v1":
        return {"reasoningEfforts": [], "thinkingEnabled": False, "frequencyPenalty": True}
    return None


def route_capabilities(route, provider):
    """Authenticated, secret-free gateway contract. Never disclose the upstream route."""
    if route_protocol(route) != "chat_completions":
        return None
    try:
        preset = get_preset(route.preset_id, route.upstream_model)
        defaults, _ = resolve_parameters(
            route.preset_id, route.upstream_model, provider.parameter_defaults, {}, route.parameter_overrides
        )
    except (ApiError, ValueError):
        # Old saved settings may no longer satisfy a revised preset. Keep the
        # catalog usable; the call validator reports the exact configuration
        # error before upstream dispatch. Never advertise unverified capabilities.
        return None
    token_field = preset["token_field"] if preset else (
        "max_completion_tokens" if "max_completion_tokens" in defaults else "max_tokens"
    )
    rules = parameter_rules(route.preset_id)
    revision = hashlib.sha256(json.dumps(
        [route.preset_id, route.upstream_model, provider.base_url, defaults, rules], sort_keys=True
    ).encode()).hexdigest()[:16]
    return {
        "protocol": "chat_completions", "version": f"v1:{revision}", "parameterAdapter": "gateway",
        "structuredOutput": ("json_schema" if "json_schema" in preset["formats"] else "json_object")
        if preset else "unknown",
        "tokenField": token_field, "defaultOutputTokens": min(defaults.get(token_field, 16384), 16384),
        "maxOutputTokens": 16384,
        **({"parameterRules": rules} if rules else {}),
    }


def get_preset(preset_id, model):
    if preset_id == "custom":
        return None
    preset = PRESETS.get(preset_id)
    if preset is None:
        raise ApiError(422, "UNKNOWN_MODEL_PRESET", "预设不存在，请重新选择或使用自定义模型")
    if model not in preset["models"]:
        raise ApiError(422, "PRESET_MODEL_MISMATCH", "模型不在该预设中，请选择自定义模型")
    return preset


def preserve_output_ceiling(result, request):
    """Configuration may lower, but never raise, an explicit caller budget."""
    requested = next((request.get(name) for name in ("max_tokens", "max_completion_tokens", "max_output_tokens")
                      if request and request.get(name) is not None), None)
    target = next((name for name in ("max_tokens", "max_completion_tokens", "max_output_tokens") if name in result), None)
    if requested is not None and target is not None and result[target] > requested:
        result[target] = requested
        return [f"{target}: capped by explicit caller output limit = {requested}"]
    return []


def resolve_parameters(preset_id, model, defaults=None, request=None, overrides=None):
    """Merge in canonical form before mapping, so aliases cannot defeat precedence."""
    preset = get_preset(preset_id, model)
    if preset is None:
        result = merge_parameters(defaults, request, overrides)
        return result, preserve_output_ceiling(result, request)
    result = deepcopy(preset["defaults"])
    for values in (defaults, request, overrides):
        layer = deepcopy(values or {})
        # Validate same-layer conflicts before normalizing aliases.
        layer = merge_parameters({}, layer, {})
        if "enable_thinking" in layer:
            layer["thinking"] = {"type": "enabled" if layer.pop("enable_thinking") else "disabled"}
        effort = layer.get("reasoning_effort")
        if effort is not None and "thinking" not in layer:
            layer["thinking"] = {"type": "disabled" if effort == "none" else "enabled"}
        if "thinking" in layer and "reasoning_effort" not in layer:
            result.pop("reasoning_effort", None)
        result = merge_parameters(result, layer, {})
    changes = preserve_output_ceiling(result, request)
    target = preset["token_field"]
    source = "max_completion_tokens" if target == "max_tokens" else "max_tokens"
    thinking = result.get("thinking", {}).get("type") == "enabled"
    if preset_id == "qwen-chat-v1" and thinking and (source in result or source in (request or {})):
        raise ApiError(
            422,
            "PRESET_PARAMETER_UNSUPPORTED",
            "该千问预设在思考模式下不能将总输出预算转为仅回答预算；请关闭思考或明确使用 max_tokens",
        )
    if source in result:
        result[target] = result.pop(source)
        changes.append(f"{source} → {target}")
    if thinking and result.get("reasoning_effort") == "none":
        raise ValueError("开启思考与 reasoning_effort=none 冲突")
    if preset_id == "qwen-chat-v1":
        if result.get("reasoning_effort") not in (None, "none"):
            raise ApiError(422, "PRESET_PARAMETER_UNSUPPORTED", "该千问预设不支持 reasoning_effort，请使用思考开关")
        if result.get("temperature", 0) >= 2:
            raise ApiError(422, "PRESET_PARAMETER_UNSUPPORTED", "千问 temperature 必须小于 2")
        if "frequency_penalty" in result:
            raise ApiError(422, "PRESET_PARAMETER_UNSUPPORTED", "该千问预设未声明 frequency_penalty，请清空此参数")
        result.pop("reasoning_effort", None)
        result.pop("thinking", None)
        result["enable_thinking"] = thinking
        changes.append("thinking → enable_thinking；关闭时省略 reasoning_effort=none")
    elif preset_id == "openai-gpt41-v1":
        if thinking or result.get("reasoning_effort") not in (None, "none"):
            raise ApiError(422, "PRESET_PARAMETER_UNSUPPORTED", "GPT-4.1 不是推理模型，请清空或关闭思考参数")
        for name in ("thinking", "reasoning_effort"):
            if name in result:
                result.pop(name)
                changes.append(f"{name}：非推理模型，省略关闭思考的无操作参数")
        if result.get(target, 1) > 32768:
            raise ApiError(422, "PRESET_OUTPUT_LIMIT", "GPT-4.1 系列输出上限为 32768 Token")
    elif preset_id == "deepseek-chat-v1":
        if result.get("reasoning_effort") == "medium":
            raise ApiError(422, "PRESET_PARAMETER_UNSUPPORTED",
                           "该预设不能等价转换 medium 推理强度，请明确选择支持的档位")
        # Validate the final mode after all layers/effort aliases are merged.
        # DeepSeek otherwise silently ignores/clamps these explicit values.
        ignored = {"temperature", "presence_penalty", "frequency_penalty"} if thinking else set()
        if "top_p" in result and (not thinking or result["top_p"] < 0.95):
            ignored.add("top_p")
        for name in sorted(ignored & result.keys()):
            if name in (request or {}):
                explanation = ("仅在思考模式下支持 0.95–1.0" if name == "top_p" else "在思考模式下不生效")
                raise ApiError(422, "PRESET_PARAMETER_UNSUPPORTED", f"DeepSeek {name} {explanation}；请移除该显式参数")
            result.pop(name)
            changes.append(f"{name}: 当前 DeepSeek 模式不支持该配置值，省略默认/强制值以免上游静默忽略或钳位")
    return result, changes


def validate_request_capabilities(preset_id, model, body):
    preset = get_preset(preset_id, model)
    if preset is None:
        return
    output = body.get("response_format")
    if output is None:
        output = {"type": "text"}
    kind = output.get("type") if isinstance(output, dict) else None
    if not isinstance(kind, str) or kind not in {"text", "json_object", "json_schema"}:
        raise ApiError(422, "INVALID_RESPONSE_FORMAT", "response_format 类型无效")
    if kind not in preset["formats"]:
        raise ApiError(400, "UPSTREAM_SCHEMA_UNSUPPORTED", "模型供应商不支持 json_schema response_format")
    if preset_id == "qwen-chat-v1" and body.get("enable_thinking") and (kind != "text" or not body.get("stream")):
        raise ApiError(422, "PRESET_THINKING_INCOMPATIBLE", "该千问预设开启思考时需流式文本；JSON 输出请关闭思考")


def route_protocol(route):
    return getattr(route, "api_protocol", None) or "chat_completions"


def validate_route_protocol(preset_id, protocol):
    if protocol not in {"chat_completions", "responses"}:
        raise ApiError(422, "INVALID_API_PROTOCOL", "请选择 Chat Completions 或 Responses")
    if protocol == "responses" and preset_id not in {"custom", "openai-gpt41-v1"}:
        raise ApiError(422, "PRESET_PROTOCOL_UNSUPPORTED", "该预设未声明支持 Responses")


def resolve_route_parameters(route, provider, request=None):
    """Map only same-semantic budgets; Responses never inherits Chat-only fields."""
    protocol = route_protocol(route)
    validate_route_protocol(route.preset_id, protocol)
    layers = [deepcopy(value or {}) for value in (provider.parameter_defaults, request, route.parameter_overrides)]
    changes = []
    if protocol == "responses":
        semantics = "total_output" if route.preset_id == "openai-gpt41-v1" else getattr(route, "budget_semantics", "unknown")
        for layer in layers:
            if "max_tokens" in layer:
                raise ApiError(422, "PARAMETER_SEMANTICS_CONFLICT", "Responses 不接受含义未确定的 max_tokens；请使用 max_output_tokens")
            if "max_completion_tokens" in layer:
                if semantics != "total_output":
                    raise ApiError(422, "PARAMETER_SEMANTICS_CONFLICT", "预算计数含义未经确认，不能转换 Completion 预算")
                layer["max_output_tokens"] = layer.pop("max_completion_tokens")
                changes.append("max_completion_tokens → max_output_tokens（总生成预算）")
            unsupported = set(layer) - {"max_output_tokens", "temperature", "top_p", "reasoning_effort"}
            if unsupported:
                raise ApiError(422, "PROTOCOL_PARAMETER_UNSUPPORTED", "Responses 配置含不支持的参数：" + ", ".join(sorted(unsupported)))
            if route.preset_id == "openai-gpt41-v1" and "reasoning_effort" in layer:
                raise ApiError(422, "PRESET_PARAMETER_UNSUPPORTED", "GPT-4.1 不支持推理强度参数")
        result = merge_parameters(*layers)
        changes += preserve_output_ceiling(result, request)
        return result, changes
    if any("max_output_tokens" in layer for layer in layers):
        raise ApiError(422, "PROTOCOL_PARAMETER_UNSUPPORTED", "Chat Completions 不接受 max_output_tokens")
    return resolve_parameters(route.preset_id, route.upstream_model, *layers)


def route_capabilities_v2(route, provider):
    if getattr(route, "capabilities_v2_enabled", True) is False:
        return None
    protocol = route_protocol(route)
    try:
        preset = get_preset(route.preset_id, route.upstream_model)
        defaults, _ = resolve_route_parameters(route, provider)
    except (ApiError, ValueError):
        return None
    declaration = getattr(route, "structured_output", "unknown") or "unknown"
    formats = preset["formats"] if preset else (
        ["text", "json_object", "json_schema"] if declaration == "json_schema" else
        ["text", "json_object"] if declaration == "json_object" else ["text"]
    )
    modes = {kind: "supported" if kind in formats else "unsupported" if preset else "unknown"
             for kind in ("json_object", "json_schema")}
    if route.preset_id == "qwen-chat-v1" and defaults.get("enable_thinking"):
        modes = {kind: "unsupported" for kind in modes}
    token_field = "max_output_tokens" if protocol == "responses" else (
        preset["token_field"] if preset else
        "max_completion_tokens" if "max_completion_tokens" in defaults else "max_tokens"
    )
    semantics = ("total_output" if route.preset_id == "openai-gpt41-v1" else
                 getattr(route, "budget_semantics", "unknown") or "unknown")
    rules = parameter_rules(route.preset_id)
    if rules:
        rules = {**rules, "temperature": "supported", "topP": "supported",
                 "presencePenalty": "supported" if route.preset_id == "openai-gpt41-v1" and protocol == "chat_completions" else "unknown"}
        if route.preset_id == "deepseek-chat-v1":
            # V2 can represent the documented max effort. The legacy DTO keeps
            # its old enum so older Core decoders remain compatible.
            rules["reasoningEfforts"] = ["low", "high", "max"]
            # top_p is conditional on thinking AND its narrower effective range.
            rules["topP"] = "conditional"
            if defaults.get("thinking", {}).get("type") == "enabled":
                rules.update(temperature="conditional", frequencyPenalty=False)
        if protocol == "responses":
            rules.update(frequencyPenalty=False, presencePenalty="unsupported", thinkingEnabled=False)
    # The digest is opaque. Provider credentials are never published; rotation
    # changes revision so stale preflight evidence cannot silently survive it.
    revision_source = ["model-capabilities@2", route.preset_id, route.upstream_model, provider.base_url,
                       getattr(provider, "encrypted_key", ""), protocol, modes, semantics, defaults, rules,
                       provider.parameter_defaults or {}, route.parameter_overrides or {},
                       "text-client-context-nonstream@1", 16384]
    revision = hashlib.sha256(json.dumps(revision_source, sort_keys=True).encode()).hexdigest()[:24]
    return {
        "version": "model-capabilities@2", "revision": f"v2:{revision}",
        "supportedProtocols": [protocol], "preferredProtocol": protocol, "parameterAdapter": "gateway",
        "outputModes": modes, "tokenField": token_field,
        "defaultOutputTokens": min(defaults.get(token_field, 16384), 16384), "maxOutputTokens": 16384,
        "budgetSemantics": semantics, "strictFlag": "required" if preset and "json_schema" in formats else "optional",
        "store": "supported" if route.preset_id == "openai-gpt41-v1" else "unknown",
        "evidence": {"source": "documented" if preset else "user_declared" if declaration != "unknown" else "unknown"},
        **({"parameterRules": rules} if rules else {}),
    }
