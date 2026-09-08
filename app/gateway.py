"""Bounded Chat Completions adapter. Configuration and metadata belong to Admin only."""

import asyncio
import ipaddress
import json
import time
from urllib.parse import urlsplit
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .config import settings
from .db import get_db
from .models import Provider, ModelRoute, ModelKey, ModelCall
from .security import ApiError, digest, token, limiter, require_admin

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
        allowed = {x.strip().lower() for x in settings().model_allowed_hosts.split(",") if x.strip()}
        if (
            u.scheme != "https"
            or not u.hostname
            or u.hostname.lower() not in allowed
            or u.username
            or u.password
            or u.query
            or u.fragment
            or u.port not in {None, 443}
        ):
            raise ValueError()
        try:
            address = ipaddress.ip_address(u.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        if u.hostname in {"localhost", "metadata.google.internal"} or ".." in u.path or "\\" in value:
            raise ValueError()
    except ValueError:
        raise ApiError(
            422, "INVALID_UPSTREAM", "上游须使用 HTTPS/443，且主机须在 MODEL_ALLOWED_HOSTS 白名单中；不允许内嵌凭据"
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
    }


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProviderInput(Strict):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str = Field(default="", max_length=4096)
    enabled: bool = True
    timeout_seconds: int = Field(default=60, ge=5, le=300)


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
    if body.api_key:
        row.encrypted_key = cipher().encrypt(body.api_key.encode()).decode()
    row.name, row.base_url, row.enabled, row.timeout_seconds = body.name, url, body.enabled, body.timeout_seconds
    db.commit()
    return provider_data(row)


@admin.post("/providers/{ident}/test")
async def test_provider(ident: str, request: Request, db=Depends(get_db)):
    row = get(db, Provider, ident)
    if not row.enabled:
        raise ApiError(409, "PROVIDER_DISABLED")
    url = destination(row.base_url)
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
    alias: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._/-]+$")
    provider_id: str = Field(min_length=1, max_length=64)
    upstream_model: str = Field(min_length=1, max_length=200)
    enabled: bool = True


@admin.get("/routes")
def routes(db=Depends(get_db)):
    return [
        {
            "id": r.id,
            "alias": r.alias,
            "provider_id": r.provider_id,
            "upstream_model": r.upstream_model,
            "enabled": r.enabled,
        }
        for r in db.scalars(select(ModelRoute).order_by(ModelRoute.alias))
    ]


def save_route(body, db, row=None):
    get(db, Provider, body.provider_id)
    existing = db.scalar(select(ModelRoute).where(ModelRoute.alias == body.alias))
    if existing and (not row or row.id != existing.id):
        raise ApiError(409, "MODEL_ALIAS_EXISTS")
    if row and row.alias != body.alias:
        raise ApiError(409, "ALIAS_IMMUTABLE", "模型别名不可改名，请新建路由并更新用户授权")
    row = row or ModelRoute()
    for key, value in body.model_dump().items():
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
    return {"object": "list", "data": [{"id": r.alias, "object": "model", "owned_by": "opsark"} for r in rows]}


@admin.get("/calls")
def calls(offset: int = 0, db=Depends(get_db)):
    if offset < 0 or offset > 100000:
        raise ApiError(422, "INVALID_OFFSET")
    rows = db.scalars(select(ModelCall).order_by(ModelCall.started_at.desc()).offset(offset).limit(100))
    return [{c.name: getattr(r, c.name) for c in ModelCall.__table__.columns} for r in rows]


def usage(call, data):
    value = data.get("usage")
    if isinstance(value, dict):
        for source, target in [("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")]:
            count = value.get(source)
            if type(count) is int and 0 <= count <= 10**9:
                setattr(call, target, count)


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
    "reasoning_effort",
    "stream_options",
    "seed",
    "presence_penalty",
    "frequency_penalty",
}


@public.post("/chat/completions")
async def completions(request: Request, key=Depends(require_key), db=Depends(get_db)):
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(422, "INVALID_JSON")
    if (
        not isinstance(body, dict)
        or set(body) - ALLOWED
        or not isinstance(body.get("model"), str)
        or not isinstance(body.get("messages"), list)
        or not body["messages"]
        or type(body.get("stream", False)) is not bool
    ):
        raise ApiError(422, "INVALID_CHAT_REQUEST")
    alias = body["model"]
    if alias not in key.allowed_models:
        raise ApiError(403, "MODEL_DENIED")
    route = db.scalar(select(ModelRoute).where(ModelRoute.alias == alias, ModelRoute.enabled.is_(True)))
    if not route:
        raise ApiError(404, "MODEL_NOT_FOUND")
    provider = get(db, Provider, route.provider_id)
    if not provider.enabled:
        raise ApiError(503, "PROVIDER_DISABLED")
    url, raw_key = destination(provider.base_url), secret(provider)
    body["model"] = route.upstream_model
    call = ModelCall(key_id=key.id, owner=key.owner, provider_id=provider.id, model=alias, started_at=time.time())
    db.add(call)
    db.commit()
    started = time.monotonic()
    response = None
    transferred = False
    acquired = False

    def finish(status, code=None, http_status=None):
        call.status, call.error_code = status, code
        if http_status is not None:
            call.http_status = http_status
        call.duration_ms = int((time.monotonic() - started) * 1000)
        db.commit()

    try:
        await asyncio.wait_for(request.app.state.model_slots.acquire(), timeout=1)
        acquired = True
        upstream = request.app.state.model_client.build_request(
            "POST",
            url + "/chat/completions",
            json=body,
            headers={
                "Authorization": "Bearer " + raw_key,
                "Accept": "text/event-stream" if body.get("stream") else "application/json",
            },
            timeout=provider.timeout_seconds,
        )
        response = await request.app.state.model_client.send(upstream, stream=True)
        call.http_status = response.status_code
        if response.status_code != 200:
            finish("failed", "UPSTREAM_HTTP_ERROR")
            return JSONResponse(
                {"error": {"code": "UPSTREAM_HTTP_ERROR", "message": "模型供应商请求失败", "request_id": call.id}},
                status_code=response.status_code if response.status_code in {400, 422, 429} else 502,
                headers={"X-Request-ID": call.id},
            )
        if body.get("stream"):
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise ValueError("invalid stream")

            async def events():
                total = 0
                pending = b""
                done = False
                try:
                    async with asyncio.timeout(provider.timeout_seconds):
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
                                usage(call, data)
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
                events(), media_type="text/event-stream", headers={"X-Request-ID": call.id, "X-Accel-Buffering": "no"}
            )
        data_bytes = bytearray()
        async with asyncio.timeout(provider.timeout_seconds):
            async for part in response.aiter_bytes():
                data_bytes.extend(part)
                if len(data_bytes) > 8 * 1024 * 1024:
                    raise ValueError("response too large")
        data = json.loads(data_bytes)
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
        usage(call, data)
        finish("succeeded")
        return JSONResponse(data, headers={"X-Request-ID": call.id})
    except asyncio.CancelledError:
        finish("cancelled", "CLIENT_DISCONNECTED")
        raise
    except (httpx.HTTPError, ValueError, TimeoutError):
        finish("failed", "UPSTREAM_UNAVAILABLE")
        return JSONResponse(
            {
                "error": {
                    "code": "UPSTREAM_UNAVAILABLE",
                    "message": "上游连接失败、超时或响应无效",
                    "request_id": call.id,
                }
            },
            status_code=502,
            headers={"X-Request-ID": call.id},
        )
    finally:
        if not transferred:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                if acquired:
                    request.app.state.model_slots.release()
