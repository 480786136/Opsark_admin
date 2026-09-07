import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .config import settings
from .db import get_db
from .models import Admin, LoginSession
from .security import ApiError, check_origin, digest, limiter, require_admin, token


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(
        base_url=settings().knowledge_url.rstrip("/") + "/", timeout=40, follow_redirects=False, trust_env=False
    ) as client:
        app.state.knowledge_client = client
        yield


app = FastAPI(
    title="Opsark Platform", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


@app.exception_handler(ApiError)
async def error(request, exc):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    return await error(request, ApiError(422, "VALIDATION_ERROR", "请求字段不正确"))


@app.middleware("http")
async def protect(request, call_next):
    if request.method in {"POST", "PATCH", "PUT"}:
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 2 * 1024 * 1024:
                return await error(request, ApiError(413, "PAYLOAD_TOO_LARGE"))
        request._body = bytes(body)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/health/live")
def live():
    return {"status": "ok", "service": "platform"}


@app.post("/api/admin/v1/session")
def login(payload: LoginInput, request: Request, response: Response, db=Depends(get_db)):
    check_origin(request)
    limiter.check("login:" + (request.client.host if request.client else "unknown"), 10, 300)
    admin = db.scalar(select(Admin).where(Admin.username == payload.username))
    try:
        if not admin or not PasswordHasher().verify(admin.password_hash, payload.password):
            raise ApiError(401, "INVALID_CREDENTIALS", "用户名或密码不正确")
    except VerificationError:
        raise ApiError(401, "INVALID_CREDENTIALS", "用户名或密码不正确")
    raw, csrf = token(), token()
    db.add(LoginSession(token_hash=digest(raw), admin_id=admin.id, csrf=csrf, expires=time.time() + 8 * 3600))
    db.commit()
    response.set_cookie(
        "opsark_session", raw, httponly=True, secure=settings().cookie_secure, samesite="strict", max_age=8 * 3600
    )
    return {"username": admin.username, "csrf": csrf}


@app.get("/api/admin/v1/session")
def current(session=Depends(require_admin), db=Depends(get_db)):
    return {"username": db.get(Admin, session.admin_id).username, "csrf": session.csrf}


@app.delete("/api/admin/v1/session")
def logout(response: Response, session=Depends(require_admin), db=Depends(get_db)):
    db.delete(session)
    db.commit()
    response.delete_cookie("opsark_session")
    return {"ok": True}


@app.get("/api/admin/v1/config")
def config(session=Depends(require_admin)):
    s = settings()
    return {
        "knowledge_base_url": s.knowledge_public_url,
        "model_base_url": s.model_base_url,
        "model_console_url": s.model_console_url,
        "knowledge_connected": len(s.knowledge_service_token) >= 32,
        "core_defaults": {"upload_enabled": False, "search_enabled": False},
    }


# Only this explicit management surface can be reached with a platform session.
ROUTES = {
    "GET": [r"health", r"knowledge-bases", r"knowledge-keys", r"records", r"documents", r"jobs", r"audit-events"],
    "POST": [
        r"knowledge-bases",
        r"knowledge-keys",
        r"documents",
        r"documents/[a-f0-9]{32}/(?:publish|unpublish)",
        r"jobs/[a-f0-9]{32}/retry",
        r"search",
    ],
    "PATCH": [r"knowledge-bases/[a-f0-9]{32}", r"documents/[a-f0-9]{32}/draft"],
    "DELETE": [r"knowledge-keys/[a-f0-9]{32}", r"documents/[a-f0-9]{32}", r"records/[a-f0-9]{32}"],
}


@app.api_route("/api/admin/v1/knowledge/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def knowledge_proxy(path: str, request: Request, session=Depends(require_admin)):
    if not any(re.fullmatch(pattern, path) for pattern in ROUTES[request.method]):
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    if len(settings().knowledge_service_token) < 32:
        raise ApiError(503, "KNOWLEDGE_NOT_CONFIGURED", "请在服务器配置知识服务地址和服务凭据")
    try:
        upstream = await request.app.state.knowledge_client.request(
            request.method,
            "internal/v1/" + path,
            content=await request.body(),
            headers={
                "Authorization": f"Bearer {settings().knowledge_service_token}",
                "X-Opsark-Actor": session.admin_id,
                "Content-Type": "application/json",
                "X-Request-ID": uuid.uuid4().hex,
            },
        )
    except httpx.RequestError:
        raise ApiError(503, "KNOWLEDGE_UNAVAILABLE", "知识服务暂时不可用，平台登录与模型配置仍可使用")
    if upstream.status_code >= 500:
        raise ApiError(503, "KNOWLEDGE_UNAVAILABLE", "知识服务处理失败")
    try:
        body = upstream.json()
    except ValueError:
        raise ApiError(502, "INVALID_KNOWLEDGE_RESPONSE")
    return JSONResponse(body, status_code=upstream.status_code)


web = Path(__file__).resolve().parent.parent / "web" / "dist"
if web.is_dir():
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
