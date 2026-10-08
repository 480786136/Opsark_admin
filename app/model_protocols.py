"""Bounded text-only Responses wire contract. No protocol translation or tool authority."""
# ValueError marks an invalid upstream envelope for the shared gateway handler.
# ruff: noqa: TRY004
from .security import ApiError

RESPONSES_ALLOWED = {"model", "input", "instructions", "stream", "text", "max_output_tokens",
                     "temperature", "top_p", "reasoning", "store"}


def output_format(body, protocol):
    value = body.get("text", {}).get("format") if protocol == "responses" else body.get("response_format")
    return value if isinstance(value, dict) else {}


def validate_responses_request(body):
    if not isinstance(body, dict) or set(body) - RESPONSES_ALLOWED or not isinstance(body.get("model"), str):
        raise ApiError(422, "INVALID_RESPONSES_REQUEST", "仅支持 Responses 文本请求及已声明参数")
    if body.get("stream", False) is not False:
        raise ApiError(422, "RESPONSES_STREAM_NOT_SUPPORTED", "当前 Responses 仅支持非流式")
    if "store" in body and body["store"] is not False:
        raise ApiError(422, "PROVIDER_STORAGE_NOT_SUPPORTED", "当前仅支持客户端管理上下文及 store=false")
    if "instructions" in body and not isinstance(body["instructions"], str):
        raise ApiError(422, "INVALID_RESPONSES_REQUEST", "instructions 必须是文本")
    items = body.get("input")
    if isinstance(items, str):
        if not items:
            raise ApiError(422, "UNSUPPORTED_MESSAGE", "input 不能为空")
    elif isinstance(items, list) and items:
        for item in items:
            if (not isinstance(item, dict) or set(item) - {"type", "role", "content"}
                    or item.get("type", "message") != "message"
                    or item.get("role") not in {"system", "developer", "user", "assistant"}):
                raise ApiError(422, "UNSUPPORTED_MESSAGE", "当前 Responses 只接受文本消息")
            content = item.get("content")
            if isinstance(content, str):
                continue
            if (not isinstance(content, list) or not content
                    or any(not isinstance(part, dict) or set(part) != {"type", "text"}
                           or part.get("type") != "input_text" or not isinstance(part.get("text"), str)
                           for part in content)):
                raise ApiError(422, "UNSUPPORTED_MESSAGE", "当前 Responses 只接受 input_text 内容")
    else:
        raise ApiError(422, "INVALID_RESPONSES_REQUEST", "Responses 需要 input 文本或消息列表")
    if "text" in body:
        text = body["text"]
        if not isinstance(text, dict) or set(text) != {"format"} or not isinstance(text.get("format"), dict):
            raise ApiError(422, "INVALID_RESPONSE_FORMAT", "text 仅支持 format")
        value = text["format"]
        kind = value.get("type")
        if kind not in {"text", "json_object", "json_schema"}:
            raise ApiError(422, "INVALID_RESPONSE_FORMAT", "text.format 类型无效")
        allowed = {"type", "name", "schema", "strict", "description"} if kind == "json_schema" else {"type"}
        if set(value) - allowed or (kind == "json_schema" and (
                not isinstance(value.get("name"), str) or not value["name"]
                or not isinstance(value.get("schema"), dict)
                or ("strict" in value and type(value["strict"]) is not bool))):
            raise ApiError(422, "INVALID_RESPONSE_FORMAT", "Responses Schema 应直接位于 text.format.schema")
    if "reasoning" in body:
        value = body["reasoning"]
        if not isinstance(value, dict) or set(value) != {"effort"} or not isinstance(value["effort"], str):
            raise ApiError(422, "INVALID_MODEL_PARAMETERS", "当前 reasoning 仅支持已声明的 effort")


def response_status(data):
    """Preserve provider status envelopes, including refusal, pending and failure."""
    if not isinstance(data, dict) or data.get("status") not in {
        "completed", "incomplete", "failed", "cancelled", "queued", "in_progress"
    } or not isinstance(data.get("output"), list):
        raise ValueError("invalid Responses envelope")
    for item in data["output"]:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ValueError("invalid Responses output item")
        if item["type"] == "message":
            if not isinstance(item.get("content"), list):
                raise ValueError("invalid Responses message")
            for part in item["content"]:
                if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                    raise ValueError("invalid Responses content")
                if part["type"] == "output_text" and not isinstance(part.get("text"), str):
                    raise ValueError("invalid Responses output text")
                if part["type"] == "refusal" and not isinstance(part.get("refusal"), str):
                    raise ValueError("invalid Responses refusal")
    return data["status"]


def upstream_url(base_url, protocol):
    """Accept an API root or the matching full endpoint; reject cross-protocol suffixes."""
    suffix = "/responses" if protocol == "responses" else "/chat/completions"
    other = "/chat/completions" if protocol == "responses" else "/responses"
    if base_url.endswith(other):
        raise ApiError(422, "UPSTREAM_PROTOCOL_MISMATCH", "完整上游地址与所选 API 协议不一致")
    return base_url if base_url.endswith(suffix) else base_url + suffix
