"""Bounded Chat Completions and Responses gateway. Configuration and metadata belong to Admin only."""

import asyncio
import ipaddress
import json
import logging
import re
import time
from types import SimpleNamespace
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .accounts import model_identity
from .call_details import snapshot
from .call_monitor import call_data, trace_metadata
from .config import settings
from .credits import begin_direct_call, reject_duplicate_request, settle
from .db import get_db
from .model_delivery import delivery_metadata, safe_provider_code
from .model_parameters import ModelParameters, validate_call_parameters
from .model_presets import (
    catalog,
    get_preset,
    resolve_route_parameters,
    route_capabilities,
    route_capabilities_v2,
    route_protocol,
    validate_request_capabilities,
)
from .model_protocols import output_format, response_status, upstream_url, validate_responses_request
from .models import ModelCall, ModelCallDetail, ModelKey, ModelRoute, Provider
from .security import ApiError, digest, limiter, require_admin, token
from .user_models import AccountAudit, UserAccount

admin = APIRouter(prefix="/api/admin/v1", dependencies=[Depends(require_admin)])
public = APIRouter(prefix="/v1")


def cipher():
    try:
        return Fernet(settings().model_key_encryption_key.encode())
    except (ValueError, TypeError):
        raise ApiError(503, "KEY_STORAGE_NOT_CONFIGURED", "请配置 MODEL_KEY_ENCRYPTION_KEY")


def destination(value):
    try:
        u = urlsplit(value)
        if (
            u.scheme not in {"http", "https"}
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
            or u.port == 0
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError()
        try:
            address = ipaddress.ip_address(u.hostname)
        except ValueError:
            address = None
        if address is not None and (
            address.is_loopback or address.is_link_local
            or address.is_unspecified or address.is_multicast
        ):
            raise ValueError()
        if u.hostname in {"localhost", "metadata.google.internal"} or ".." in u.path or "\\" in value:
            raise ValueError()
    except ValueError:
        raise ApiError(
            422, "INVALID_UPSTREAM", "上游须为有效 HTTP/HTTPS 地址，可使用内网 IP 和自定义端口；"
            "不允许内嵌凭据、查询参数、片段或本机/链路本地地址"
        )
    return value.rstrip("/")


def secret(provider):
    try:
        return cipher().decrypt(provider.encrypted_key.encode()).decode()
    except InvalidToken:
        raise ApiError(503, "KEY_DECRYPT_FAILED", "上游密钥无法解密，请检查加密主密钥")


def get(db, cls, ident):
    row = db.get(cls, ident)
    if not row:
        raise ApiError(404, "NOT_FOUND")
    return row


def provider_data(p):
    return {
        "id": p.id,
        "name": p.name,
        "base_url": p.base_url,
        "enabled": p.enabled,
        "timeout_seconds": p.timeout_seconds,
        "has_key": bool(p.encrypted_key),
        "protocol": "chat_completions",
        "parameter_defaults": p.parameter_defaults or {},
    }


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProviderInput(Strict):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str = Field(default="", max_length=4096)
    enabled: bool = True
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    parameter_defaults: ModelParameters = Field(default_factory=ModelParameters)


@admin.get("/providers")
def providers(db=Depends(get_db)):
    return [provider_data(p) for p in db.scalars(select(Provider).order_by(Provider.name))]


@admin.post("/providers", status_code=201)
def create_provider(body: ProviderInput, db=Depends(get_db)):
    url = destination(body.base_url)
    if not body.api_key:
        raise ApiError(422, "KEY_REQUIRED")
    row = Provider(
        name=body.name,
        base_url=url,
        encrypted_key=cipher().encrypt(body.api_key.encode()).decode(),
        enabled=body.enabled,
        timeout_seconds=body.timeout_seconds,
        parameter_defaults=body.parameter_defaults.compact(),
    )
    db.add(row)
    db.commit()
    return provider_data(row)


@admin.put("/providers/{ident}")
def edit_provider(ident: str, body: ProviderInput, db=Depends(get_db)):
    row = get(db, Provider, ident)
    url = destination(body.base_url)
    if url != row.base_url and not body.api_key:
        raise ApiError(422, "KEY_REQUIRED_FOR_NEW_DESTINATION", "更换地址需重新输入上游 Key")
    for route in db.scalars(select(ModelRoute).where(ModelRoute.provider_id == ident)):
        try:
            upstream_url(url, route_protocol(route))
            resolve_route_parameters(route, SimpleNamespace(parameter_defaults=body.parameter_defaults.compact()))
        except ValueError:
            raise ApiError(422, "INVALID_MODEL_PARAMETERS", "供应商默认值与已有路由冲突")
    if body.api_key:
        row.encrypted_key = cipher().encrypt(body.api_key.encode()).decode()
    row.name, row.base_url, row.enabled, row.timeout_seconds = body.name, url, body.enabled, body.timeout_seconds
    row.parameter_defaults = body.parameter_defaults.compact()
    db.commit()
    return provider_data(row)


@admin.post("/providers/{ident}/test")
async def test_provider(ident: str, request: Request, db=Depends(get_db)):
    row = get(db, Provider, ident)
    if not row.enabled:
        raise ApiError(409, "PROVIDER_DISABLED")
    url = destination(row.base_url)
    for suffix in ("/chat/completions", "/responses"):
        if url.endswith(suffix):
            url = url[:-len(suffix)]
            break
    started = time.monotonic()
    try:
        async with request.app.state.model_client.stream(
            "GET", url + "/models", headers={"Authorization": "Bearer " + secret(row)}, timeout=row.timeout_seconds
        ) as response:
            status = response.status_code
    except httpx.RequestError:
        raise ApiError(502, "UPSTREAM_UNAVAILABLE")
    return {"ok": status == 200, "http_status": status, "duration_ms": int((time.monotonic() - started) * 1000)}


class RouteInput(Strict):
    display_name: str = Field(default="", max_length=100)
    alias: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._/-]+$")
    provider_id: str = Field(min_length=1, max_length=64)
    upstream_model: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    parameter_overrides: ModelParameters = Field(default_factory=ModelParameters)
    preset_id: str = Field(default="custom", min_length=1, max_length=64)
    api_protocol: Literal["chat_completions", "responses"] = "chat_completions"
    structured_output: Literal["unknown", "json_object", "json_schema"] = "unknown"
    budget_semantics: Literal["unknown", "total_output", "visible_output"] = "unknown"


@admin.get("/model-presets")
def model_presets():
    return catalog()


class PresetPreview(Strict):
    preset_id: str = Field(min_length=1, max_length=64)
    upstream_model: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=64)
    parameter_overrides: ModelParameters = Field(default_factory=ModelParameters)
    api_protocol: Literal["chat_completions", "responses"] = "chat_completions"
    structured_output: Literal["unknown", "json_object", "json_schema"] = "unknown"
    budget_semantics: Literal["unknown", "total_output", "visible_output"] = "unknown"


@admin.post("/model-presets/preview")
def preview_preset(body: PresetPreview, db: Annotated[Session, Depends(get_db)]):
    provider = get(db, Provider, body.provider_id)
    try:
        parameters, changes = resolve_route_parameters(
            SimpleNamespace(**{**body.model_dump(), "parameter_overrides": body.parameter_overrides.compact()}), provider
        )
    except ValueError:
        raise ApiError(422, "INVALID_MODEL_PARAMETERS", "模型高级参数组合无效")
    return {
        "parameters": parameters,
        "changes": changes,
        "protocol": body.api_protocol,
        "note": "仅预览无调用参数时的配置合并，不调用上游；实际请求还会检查流式及输出格式。",
    }


@admin.get("/routes")
def routes(db=Depends(get_db)):
    return [
        {
            "id": r.id,
            "alias": r.alias,
            "display_name": r.display_name or r.alias,
            "provider_id": r.provider_id,
            "upstream_model": r.upstream_model,
            "enabled": r.enabled,
            "parameter_overrides": r.parameter_overrides or {},
            "preset_id": r.preset_id,
            "api_protocol": route_protocol(r),
            "structured_output": r.structured_output,
            "budget_semantics": r.budget_semantics,
        }
        for r in db.scalars(select(ModelRoute).order_by(ModelRoute.alias))
    ]


def save_route(body, db, row=None):
    provider = get(db, Provider, body.provider_id)
    existing = db.scalar(select(ModelRoute).where(ModelRoute.alias == body.alias))
    if existing and (not row or row.id != existing.id):
        raise ApiError(409, "MODEL_ALIAS_EXISTS")
    if row and row.alias != body.alias:
        raise ApiError(409, "ALIAS_IMMUTABLE", "模型别名不可改名，请新建路由并更新用户授权")
    row = row or ModelRoute()
    values = body.model_dump()
    values["parameter_overrides"] = body.parameter_overrides.compact()
    if "preset_id" not in body.model_fields_set:
        values["preset_id"] = row.preset_id or "custom"
    for field in ("api_protocol", "structured_output", "budget_semantics"):
        if field not in body.model_fields_set and getattr(row, field, None):
            values[field] = getattr(row, field)
    try:
        upstream_url(destination(provider.base_url), values["api_protocol"])
        resolve_route_parameters(SimpleNamespace(**values), provider)
    except ValueError:
        raise ApiError(422, "INVALID_MODEL_PARAMETERS", "模型高级参数组合无效")
    if (values["preset_id"] != "custom" or values["api_protocol"] == "responses"
            or values["structured_output"] != "unknown"):
        row.capabilities_v2_enabled = True
    for key, value in values.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return {"id": row.id}


@admin.post("/routes", status_code=201)
def create_route(body: RouteInput, db=Depends(get_db)):
    return save_route(body, db)


@admin.put("/routes/{ident}")
def update_route(ident: str, body: RouteInput, db=Depends(get_db)):
    return save_route(body, db, get(db, ModelRoute, ident))


class KeyInput(Strict):
    owner: str = Field(min_length=1, max_length=128)
    allowed_models: list[str] = Field(min_length=1, max_length=100)
    expires_days: int = Field(default=30, ge=1, le=365)
    rpm: int = Field(default=30, ge=1, le=600)


@admin.get("/model-keys")
def keys(db=Depends(get_db)):
    return [
        {
            "id": k.id,
            "owner": k.owner,
            "prefix": k.prefix,
            "allowed_models": k.allowed_models,
            "expires": k.expires,
            "revoked": k.revoked,
            "rpm": k.rpm,
        }
        for k in db.scalars(select(ModelKey).order_by(ModelKey.expires.desc()).limit(500))
    ]


@admin.post("/model-keys", status_code=201)
def create_key(body: KeyInput, db=Depends(get_db)):
    aliases = set(db.scalars(select(ModelRoute.alias).where(ModelRoute.enabled.is_(True))))
    if not set(body.allowed_models).issubset(aliases):
        raise ApiError(422, "UNKNOWN_MODEL")
    raw = "omk_" + token()
    row = ModelKey(
        owner=body.owner,
        token_hash=digest(raw),
        prefix=raw[:12],
        allowed_models=sorted(set(body.allowed_models)),
        expires=time.time() + body.expires_days * 86400,
        rpm=body.rpm,
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "api_key": raw}


@admin.delete("/model-keys/{ident}")
def revoke_key(ident: str, db=Depends(get_db)):
    get(db, ModelKey, ident).revoked = True
    db.commit()
    return {"revoked": True}


def require_key(request: Request, db=Depends(get_db)):
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ouc_"):
        return model_identity(request, db)
    key = (
        db.scalar(select(ModelKey).where(ModelKey.token_hash == digest(header[7:])))
        if header.startswith("Bearer omk_")
        else None
    )
    if not key or key.revoked or key.expires <= time.time():
        raise ApiError(401, "INVALID_MODEL_KEY")
    limiter.check("model:" + key.id, key.rpm)
    return key


@public.get("/models")
def model_list(key=Depends(require_key), db=Depends(get_db)):
    rows = db.scalars(
        select(ModelRoute)
        .join(Provider)
        .where(ModelRoute.enabled.is_(True), Provider.enabled.is_(True), ModelRoute.alias.in_(key.allowed_models))
    )
    return {"object": "list", "data": [
        {"id": r.alias, "name": r.display_name or r.alias, "object": "model", "owned_by": "opsark",
         "capabilities": route_capabilities(r, get(db, Provider, r.provider_id)),
         "capabilitiesV2": route_capabilities_v2(r, get(db, Provider, r.provider_id))} for r in rows
    ]}


@admin.get("/calls")
def calls(offset: int = 0, db=Depends(get_db)):
    if offset < 0 or offset > 100000:
        raise ApiError(422, "INVALID_OFFSET")
    rows = db.scalars(select(ModelCall).order_by(ModelCall.started_at.desc()).offset(offset).limit(100))
    return [{c.name: getattr(r, c.name) for c in ModelCall.__table__.columns} for r in rows]


@admin.get("/calls/{ident}")
def call_detail(ident: str, session=Depends(require_admin), db=Depends(get_db)):
    call = get(db, ModelCall, ident)
    db.execute(delete(ModelCallDetail).where(ModelCallDetail.expires_at <= time.time()))
    db.commit()
    row = db.get(ModelCallDetail, ident)
    detail = None
    if row:
        try:
            detail = json.loads(cipher().decrypt(row.encrypted_content.encode()))
        except InvalidToken:
            raise ApiError(503, "DETAIL_DECRYPT_FAILED", "详情解密失败，请检查加密主密钥")
    db.add(AccountAudit(actor_id=session.admin_id, action="model_call_viewed",
                        detail={"call_id": ident, "user_id": call.user_id, "task_id": call.task_id,
                                "snapshot_available": detail is not None}, created_at=time.time()))
    db.commit()
    return {
        "call": call_data(call, db.scalar(select(UserAccount.email).where(UserAccount.id == call.user_id)),
                          db.scalar(select(Provider.name).where(Provider.id == call.provider_id))),
        "detail": detail,
        "detail_policy": "redacted_snapshot",
        "expires_at": row.expires_at if row else None,
    }


def usage(call, data, protocol="chat_completions"):
    value = data.get("usage")
    if not isinstance(value, dict):
        return
    # No missing, negative, boolean or fractional value is turned into zero.
    fields = [("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")] if protocol == "responses" else [
        ("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")]
    for source, target in fields:
        count = value.get(source)
        if type(count) is int and 0 <= count <= 10**9:
            setattr(call, target, count)
    for source, target, parent in [
        ("cached_tokens", "cached_tokens", "input_tokens_details" if protocol == "responses" else "prompt_tokens_details"),
        ("reasoning_tokens", "reasoning_tokens", "output_tokens_details" if protocol == "responses" else "completion_tokens_details"),
    ]:
        details = value.get(parent)
        count = details.get(source) if isinstance(details, dict) else None
        if type(count) is int and 0 <= count <= 10**9:
            setattr(call, target, count)
    # The details are diagnostic components, never added again to billed totals.
    call.usage_raw = {key: val if type(val) is int else {
        name: count for name, count in val.items() if isinstance(name, str) and len(name) <= 64 and type(count) is int
    } for key, val in value.items() if key in {
        "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
        "input_tokens_details", "output_tokens_details", "prompt_tokens_details", "completion_tokens_details"}
        and (type(val) is int or isinstance(val, dict))}


def finish_reason(choice):
    value = choice.get("finish_reason")
    return value if value in ("stop", "length", "tool_calls", "function_call", "content_filter") else "other"


ALLOWED = {
    "model",
    "messages",
    "stream",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "thinking",
    "enable_thinking",
    "reasoning_effort",
    "stream_options",
    "store",
    "seed",
    "presence_penalty",
    "frequency_penalty",
}


def schema_definition_invalid(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    if error.get("code") in {"invalid_schema", "invalid_json_schema"}:
        return True
    message = error.get("message")
    if not isinstance(message, str):
        return False
    message = message.lower()
    if "schema" not in message and "response_format" not in message:
        return False
    return any(marker in message for marker in (
        "invalid schema", "invalid json schema", "invalid json_schema", "schema is invalid",
        "schema validation", "keyword", "in context", "schema for", "schema must", "schema should",
        "not permitted", "not allowed", "additionalproperties", "additional_properties",
        "allof", "oneof", "anyof", "minitems", "maxitems", "minlength", "maxlength",
        "patternproperties", "minimum", "maximum", "multipleof", "uniqueitems", "$ref", "$defs",
        "schema不合法", "schema 无效", "无效的 schema", "不支持的关键字", "关键字不支持",
        "'not'", '"not"', "'if'", "'then'", "'else'",
    ))


def schema_capability_refused(payload):
    """Classify only explicit format support errors; never expose upstream text."""
    if schema_definition_invalid(payload):
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return False
    message = message.lower()
    # DeepSeek Chat's explicit capability error. Do not classify a generic
    # service outage containing response_format as an unsupported capability.
    if re.search(r"\b(?:response_format\s+type|json_schema)\s+is\s+unavailable\s+now\b", message):
        return True
    return any(name in message for name in ("json_schema", "response_format", "text.format")) and any(
        word in message for word in ("not supported", "unsupported", "does not support", "不支持")
    )


@public.post("/responses")
@public.post("/chat/completions")
async def completions(request: Request, key=Depends(require_key), db=Depends(get_db)):
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(422, "INVALID_JSON")
    protocol = "responses" if request.url.path.endswith("/responses") else "chat_completions"
    if protocol == "responses":
        validate_responses_request(body)
    elif (
        not isinstance(body, dict)
        or set(body) - ALLOWED
        or not isinstance(body.get("model"), str)
        or not isinstance(body.get("messages"), list)
        or not body["messages"]
        or type(body.get("stream", False)) is not bool
    ):
        raise ApiError(422, "INVALID_CHAT_REQUEST")
    if "store" in body and body["store"] is not False:
        raise ApiError(422, "PROVIDER_STORAGE_NOT_SUPPORTED", "当前仅支持 store=false")
    alias = body["model"]
    user_id = getattr(key, "user_id", None)
    # Preserve pre-J3 Chat hashes so an existing in-flight request key still
    # resolves to its original call after upgrade. Responses uses a namespaced hash.
    request_hash = digest(json.dumps(body if protocol == "chat_completions" else {
        "protocol": protocol, "body": body}, sort_keys=True, ensure_ascii=False))
    idempotency_key = request.headers.get("idempotency-key", "")
    if user_id:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,128}", idempotency_key):
            raise ApiError(422, "IDEMPOTENCY_KEY_REQUIRED", "官方模型调用必须携带请求标识")
        if body.get("stream"):
            raise ApiError(422, "USER_STREAM_NOT_SUPPORTED", "当前官方模型仅支持非流式调用")
        if protocol == "chat_completions" and any(not isinstance(m, dict) or not isinstance(m.get("content"), str)
               or m.get("role") not in {"system", "user", "assistant"} for m in body["messages"]):
            raise ApiError(422, "UNSUPPORTED_MESSAGE", "当前官方模型只接受文本消息")
    if alias not in key.allowed_models:
        raise ApiError(403, "MODEL_DENIED")
    route = db.scalar(select(ModelRoute).where(ModelRoute.alias == alias, ModelRoute.enabled.is_(True)))
    if not route:
        raise ApiError(404, "MODEL_NOT_FOUND")
    provider = get(db, Provider, route.provider_id)
    if not provider.enabled:
        raise ApiError(503, "PROVIDER_DISABLED")
    if route_protocol(route) != protocol:
        raise ApiError(409, "API_PROTOCOL_MISMATCH", "所选路由不支持当前 API 协议", details={
            "origin": "gateway", "stage": "request_capability", "dispatchCertainty": "not_dispatched"})
    capabilities_v2 = route_capabilities_v2(route, provider)
    expected_revision = request.headers.get("x-opsark-expected-capability-revision")
    if expected_revision and (not capabilities_v2 or expected_revision != capabilities_v2["revision"]):
        raise ApiError(409, "CAPABILITY_REVISION_MISMATCH", "路由能力已变化，请刷新模型配置后重新检查", details={
            "origin": "gateway", "stage": "request_capability", "dispatchCertainty": "not_dispatched"})
    url, raw_key = upstream_url(destination(provider.base_url), protocol), secret(provider)
    try:
        parameter_body = dict(body)
        if protocol == "responses" and "reasoning" in body:
            parameter_body["reasoning_effort"] = body["reasoning"]["effort"]
        request_parameters = validate_call_parameters(parameter_body)
        requested_parameters = {**request_parameters, "response_format": output_format(body, protocol).get("type")}
        effective_parameters, parameter_changes = resolve_route_parameters(route, provider, request_parameters)
    except ValueError:
        raise ApiError(422, "INVALID_MODEL_PARAMETERS", "模型高级参数组合无效")
    for name in request_parameters:
        body.pop(name, None)
    body.update(effective_parameters)
    if protocol == "responses":
        body.pop("reasoning", None)
        if "reasoning_effort" in body:
            body["reasoning"] = {"effort": body.pop("reasoning_effort")}
    body["model"] = route.upstream_model
    capability_error = None
    try:
        if protocol == "chat_completions":
            validate_request_capabilities(route.preset_id, route.upstream_model, body)
        if protocol == "responses" or expected_revision:
            mode = output_format(body, protocol).get("type", "text")
            if mode != "text" and (not capabilities_v2 or capabilities_v2["outputModes"].get(mode) != "supported"):
                raise ApiError(422, "OUTPUT_CAPABILITY_UNKNOWN", "当前路由尚未声明支持本次输出格式")
    except ApiError as error:
        capability_error = error
    if user_id or protocol == "responses" or expected_revision:
        preset = get_preset(route.preset_id, route.upstream_model)
        output_field = "max_output_tokens" if protocol == "responses" else (
            preset["token_field"] if preset else "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens")
        max_output = body.get(output_field, 16384)
        if type(max_output) is not int or not 1 <= max_output <= 16384:
            raise ApiError(422, "OUTPUT_LIMIT", "官方模型输出上限为 16384 Token")
        body.pop("max_tokens", None)
        body.pop("max_completion_tokens", None)
        body[output_field] = max_output
        if output_field not in effective_parameters:
            parameter_changes.append(f"{output_field}: gateway default = {max_output}")
        # This is a request-size safety limit, not a token estimator.
        input_size = len(json.dumps(body, ensure_ascii=False).encode())
        if input_size > 256 * 1024:
            raise ApiError(413, "OFFICIAL_CONTEXT_TOO_LARGE", "官方模型单次上下文过大")
    call = ModelCall(key_id=key.id, owner=key.owner, user_id=user_id, provider_id=provider.id,
                     model=alias, api_protocol=protocol, started_at=time.time(), **trace_metadata(request))
    db.add(call)
    try:
        db.flush()
        if user_id:
            begin_direct_call(db, user_id, call.id, idempotency_key, request_hash)
        db.commit()
    except ApiError as error:
        db.rollback()
        if error.code in {"INSUFFICIENT_CREDITS", "CREDITS_RECONCILIATION_REQUIRED"}:
            # Admission failures consume no credits. Preserve metadata only in
            # a separate transaction so the monitor can explain denied calls.
            call.status, call.error_code, call.http_status = "failed", error.code, error.status
            call.duration_ms = 0
            try:
                db.add(call)
                db.commit()
                error.details = {**(error.details or {}), "request_id": call.id}
            except Exception as audit_error:  # noqa: BLE001 - optional audit must not hide the admission error
                db.rollback()
                logging.getLogger(__name__).error("credit_rejection_audit_failed exception_type=%s",
                                                 type(audit_error).__name__)
        raise
    except IntegrityError:
        db.rollback()
        if user_id:
            # An overlapping transaction can win the unique-key insert after
            # admission checked it. Re-read and compare the winning hash too.
            reject_duplicate_request(db, user_id, idempotency_key, request_hash)
        raise ApiError(409, "REQUEST_STATE_CONFLICT", "请求记录发生冲突，请核对原请求状态",
                       details={"origin": "gateway", "stage": "request_deduplication",
                                "gatewayHttpStatus": 409, "dispatchCertainty": "not_dispatched",
                                **({"requestKey": idempotency_key} if idempotency_key else {})},
                       retryable=False)
    started = time.monotonic()
    response = None
    transferred = False
    acquired = False
    captured_output = None
    finish_reasons = []
    stream_output = {}
    # All accepted gateway calls, including official user calls, share the
    # server-side redacted/encrypted capture policy. Never collect HTTP headers.
    capture = settings().model_call_capture_enabled
    dispatched = False
    secrets = (raw_key, request.headers.get("authorization", "")[7:])
    raw_client_timeout = request.headers.get("x-opsark-timeout-seconds")
    try:
        client_timeout = int(raw_client_timeout) if raw_client_timeout is not None else None
    except ValueError:
        client_timeout = None
    # A desktop model profile owns its request deadline. The provider value is
    # only the fallback for callers that do not send a bounded deadline.
    effective_timeout = (
        max(1, min(client_timeout, 900))
        if client_timeout is not None
        else provider.timeout_seconds
    )

    def error_response(code, message, *, gateway_status=502, certainty=None, parsed_error=None):
        return JSONResponse(
            {"error": {"code": code, "message": message, "request_id": call.id, "retryable": False,
                       **delivery_metadata(call, idempotency_key, gateway_status=gateway_status,
                                           certainty=certainty),
                       **({"providerCode": safe_provider_code(parsed_error)}
                          if safe_provider_code(parsed_error) else {})}},
            status_code=gateway_status, headers={"X-Request-ID": call.id},
        )

    def finish(status, code=None, http_status=None):
        call.status, call.error_code = status, code
        if http_status is not None:
            call.http_status = http_status
        call.duration_ms = int((time.monotonic() - started) * 1000)
        if user_id:
            actual = call.input_tokens + call.output_tokens if (
                call.input_tokens is not None and call.output_tokens is not None
            ) else None
            settle(db, call.id, actual, no_dispatch=not dispatched)
        db.commit()
        # Failure of optional diagnostics must not turn a successful model call into a failure.
        try:
            with db.begin_nested():
                db.execute(delete(ModelCallDetail).where(ModelCallDetail.expires_at <= time.time()))
                if capture:
                    detail = {
                        "input": snapshot(body, secrets),
                        "output": snapshot(captured_output, secrets),
                        "finish_reasons": finish_reasons,
                        "stream": bool(body.get("stream")),
                        "preset_id": route.preset_id,
                        "api_protocol": protocol,
                        "capability_revision": capabilities_v2["revision"] if capabilities_v2 else None,
                        "parameter_changes": parameter_changes,
                        "requested_parameters": requested_parameters,
                        "effective_parameters": {name: body[name] for name in (
                            "max_tokens", "max_completion_tokens", "max_output_tokens", "thinking", "enable_thinking", "reasoning",
                            "reasoning_effort", "temperature", "top_p", "frequency_penalty", "presence_penalty"
                        ) if name in body},
                        "effective_output_mode": output_format(body, protocol).get("type"),
                    }
                    db.merge(
                        ModelCallDetail(
                            call_id=call.id,
                            encrypted_content=cipher()
                            .encrypt(json.dumps(detail, ensure_ascii=False).encode())
                            .decode(),
                            expires_at=time.time() + max(1, min(settings().model_call_retention_days, 90)) * 86400,
                        )
                    )
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.getLogger(__name__).error(
                "call_detail_save_failed request_id=%s exception_type=%s", call.id, type(exc).__name__
            )

    try:
        if capability_error:
            # Record the rejection and release any reservation without dispatch.
            finish("failed", capability_error.code, capability_error.status)
            return error_response(capability_error.code, capability_error.message,
                                  gateway_status=capability_error.status, certainty="not_dispatched")
        await asyncio.wait_for(request.app.state.model_slots.acquire(), timeout=1)
        acquired = True
        upstream = request.app.state.model_client.build_request(
            "POST",
            url,
            json=body,
            headers={
                "Authorization": "Bearer " + raw_key,
                "Accept": "text/event-stream" if body.get("stream") else "application/json",
            },
            timeout=effective_timeout,
        )
        dispatched = True
        response = await request.app.state.model_client.send(upstream, stream=True)
        call.http_status = response.status_code
        if not 200 <= response.status_code < 300:
            response_format = output_format(body, protocol)
            schema_probe = (
                response.status_code in {400, 422}
                and isinstance(response_format, dict)
                and response_format.get("type") == "json_schema"
            )
            parsed_error = None
            # Parse a bounded envelope for safe machine diagnostics even when capture is disabled.
            error_body = bytearray()
            try:
                async with asyncio.timeout(effective_timeout):
                    async for part in response.aiter_bytes():
                        error_body.extend(part)
                        if len(error_body) > 64 * 1024:
                            break
                # Do not persist a cut credential or an HTML proxy error page.
                if len(error_body) <= 64 * 1024:
                    parsed_error = json.loads(error_body)
                    if capture:
                        captured_output = parsed_error
            except (ValueError, httpx.HTTPError, TimeoutError):
                if capture:
                    captured_output = {"note": "错误正文不是有效 JSON，或读取超时；未保存"}
            invalid_schema = schema_probe and schema_definition_invalid(parsed_error)
            unsupported_schema = schema_probe and schema_capability_refused(parsed_error)
            error_code = ("UPSTREAM_SCHEMA_INVALID" if invalid_schema else
                          "UPSTREAM_SCHEMA_UNSUPPORTED" if unsupported_schema else "UPSTREAM_HTTP_ERROR")
            message = ("模型供应商拒绝当前 Schema 定义" if invalid_schema else
                       "模型供应商不支持 json_schema response_format" if unsupported_schema else "模型供应商请求失败")
            finish("failed", error_code)
            return error_response(error_code, message,
                                  gateway_status=(response.status_code
                                                  if response.status_code in {400, 422, 429} else 502),
                                  certainty="response_received", parsed_error=parsed_error)
        if body.get("stream"):
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise ValueError("invalid stream")

            async def events():
                nonlocal captured_output
                total = 0
                pending = b""
                done = False
                try:
                    async with asyncio.timeout(effective_timeout):
                        async for part in response.aiter_bytes():
                            total += len(part)
                            if total > 16 * 1024 * 1024:
                                raise ValueError("stream too large")
                            pending += part
                            if len(pending) > 1024 * 1024:
                                raise ValueError("event too large")
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                line = line.strip()
                                if not line.startswith(b"data:"):
                                    continue
                                value = line[5:].strip()
                                if value == b"[DONE]":
                                    done = True
                                    finish("succeeded")
                                    yield "data: [DONE]\n\n"
                                    return
                                data = json.loads(value)
                                if (
                                    not isinstance(data, dict)
                                    or "error" in data
                                    or not isinstance(data.get("choices"), list)
                                ):
                                    raise ValueError("invalid stream event")
                                data["model"] = alias
                                usage(call, data, protocol)
                                for choice in data["choices"]:
                                    if not isinstance(choice, dict):
                                        raise ValueError("invalid choice")
                                    if choice.get("finish_reason"):
                                        finish_reasons.append(finish_reason(choice))
                                    if capture:
                                        entry = stream_output.setdefault(
                                            str(choice.get("index", 0)),
                                            {"content": "", "reasoning_content": "", "tool_calls": {}},
                                        )
                                        delta = choice.get("delta") or {}
                                        if not isinstance(delta, dict):
                                            raise ValueError("invalid delta")
                                        for field in ("content", "reasoning_content"):
                                            if isinstance(delta.get(field), str):
                                                entry[field] += delta[field]
                                        for tool in delta.get("tool_calls") or []:
                                            if not isinstance(tool, dict) or not isinstance(
                                                tool.get("function", {}), dict
                                            ):
                                                raise ValueError("invalid tool delta")
                                            target = entry["tool_calls"].setdefault(
                                                str(tool.get("index", 0)), {"name": "", "arguments": ""}
                                            )
                                            for field in ("name", "arguments"):
                                                value = (tool.get("function") or {}).get(field) or ""
                                                if not isinstance(value, str):
                                                    raise ValueError("invalid tool text")
                                                target[field] += value
                                captured_output = stream_output if capture else None
                                yield "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
                        if not done:
                            raise ValueError("incomplete stream")
                except asyncio.CancelledError:
                    finish("cancelled", "CLIENT_DISCONNECTED")
                    raise
                except (httpx.HTTPError, ValueError, TimeoutError):
                    finish("failed", "STREAM_INTERRUPTED")
                    yield 'data: {"error":{"code":"STREAM_INTERRUPTED","message":"上游流中断，请检查调用记录"}}\n\n'
                finally:
                    try:
                        await response.aclose()
                    finally:
                        request.app.state.model_slots.release()

            transferred = True
            return StreamingResponse(
                events(), media_type="text/event-stream", headers={
                    "X-Request-ID": call.id, "X-Accel-Buffering": "no", "X-Opsark-Call-Id": call.id,
                    "X-Opsark-Provider-Http-Status": str(response.status_code),
                },
            )
        data_bytes = bytearray()
        async with asyncio.timeout(effective_timeout):
            async for part in response.aiter_bytes():
                data_bytes.extend(part)
                if len(data_bytes) > 8 * 1024 * 1024:
                    raise ValueError("response too large")
        captured_output = data_bytes.decode("utf-8", errors="replace") if capture else None
        data = json.loads(data_bytes)
        captured_output = data if capture else None
        # A valid provider usage report remains billable when the outer model
        # envelope or the business JSON content is invalid.
        if isinstance(data, dict):
            usage(call, data, protocol)
        if protocol == "responses":
            call.raw_status = response_status(data)
            data["model"] = alias
            finish_reasons.append(call.raw_status)
            finish("succeeded" if call.raw_status == "completed" else "failed",
                   None if call.raw_status == "completed" else "UPSTREAM_RESPONSE_" + call.raw_status.upper())
            return JSONResponse(data, status_code=response.status_code, headers={
                "X-Request-ID": call.id, "X-Opsark-Call-Id": call.id,
                "X-Opsark-Provider-Http-Status": str(response.status_code),
            })
        if (
            not isinstance(data, dict)
            or "error" in data
            or not isinstance(data.get("choices"), list)
            or not data["choices"]
            or not all(
                isinstance(choice, dict) and isinstance(choice.get("message"), dict) for choice in data["choices"]
            )
        ):
            raise ValueError("invalid response")
        data["model"] = alias
        usage(call, data, protocol)
        finish_reasons.extend(finish_reason(c) for c in data["choices"] if c.get("finish_reason"))
        finish("succeeded")
        return JSONResponse(data, status_code=response.status_code, headers={
            "X-Request-ID": call.id, "X-Opsark-Call-Id": call.id,
            "X-Opsark-Provider-Http-Status": str(response.status_code),
        })
    except asyncio.CancelledError:
        finish("cancelled", "CLIENT_DISCONNECTED")
        raise
    except (httpx.HTTPError, ValueError, TimeoutError) as error:
        if not acquired:
            code, message, dispatched = "GATEWAY_BUSY", "网关当前繁忙，尚未发送模型请求", False
        elif isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            code = ("UPSTREAM_CONNECT_TIMEOUT" if isinstance(error, httpx.TimeoutException)
                    else "UPSTREAM_CONNECT_FAILED")
            message, dispatched = "连接模型供应商失败，尚未发送请求", False
        elif isinstance(error, (httpx.WriteError, httpx.WriteTimeout)):
            code, message = "UPSTREAM_WRITE_FAILED", "发送模型请求失败，供应商是否受理尚不确定"
        elif isinstance(error, (httpx.TimeoutException, TimeoutError)):
            code, message = "UPSTREAM_TIMEOUT", "等待模型供应商响应超时，请查询原请求状态"
        elif isinstance(error, ValueError):
            code, message = "UPSTREAM_RESPONSE_INVALID", "模型供应商返回的响应信封无效"
        else:
            code, message = "UPSTREAM_READ_FAILED", "读取模型供应商响应失败，请查询原请求状态"
        finish("failed", code)
        return error_response(code, message,
                              certainty="not_dispatched" if not dispatched else
                              "response_received" if response is not None else "may_have_dispatched")
    finally:
        if not transferred:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                if acquired:
                    request.app.state.model_slots.release()
