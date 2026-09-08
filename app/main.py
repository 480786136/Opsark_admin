import asyncio
import time
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
        timeout=60,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    ) as client:
        app.state.model_client = client
        app.state.model_slots = asyncio.Semaphore(8)
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
    return {
        "model_base_url": "/v1",
        "protocol": "chat_completions",
        "encryption_configured": bool(settings().model_key_encryption_key),
        "allowed_hosts": [x.strip() for x in settings().model_allowed_hosts.split(",") if x.strip()],
    }


@app.api_route("/api/admin/v1/knowledge/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
def retired_knowledge(path: str, session=Depends(require_admin)):
    raise ApiError(410, "KNOWLEDGE_MOVED", "知识管理已独立，请访问知识服务自己的管理页面")


from .gateway import admin as model_admin, public as model_public  # noqa: E402

app.include_router(model_admin)
app.include_router(model_public)

web = Path(__file__).resolve().parent.parent / "web" / "dist"
if web.is_dir():
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
