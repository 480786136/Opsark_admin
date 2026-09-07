import hashlib
import secrets
import time
from collections import defaultdict, deque
from threading import Lock
from fastapi import Depends, Request
from .config import settings
from .db import get_db
from .models import LoginSession


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "请求无法完成", fields=None):
        self.status, self.code, self.message, self.fields = status, code, message, fields or []


def digest(value: str | bytes):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def token():
    return secrets.token_urlsafe(32)


class Limiter:
    """Single API process only; state is bounded and not a distributed quota."""

    def __init__(self):
        self.entries = defaultdict(deque)
        self.lock = Lock()

    def check(self, key, limit, seconds=60):
        now = time.monotonic()
        with self.lock:
            if len(self.entries) > 10000:
                self.entries = defaultdict(deque, {k: v for k, v in self.entries.items() if v and v[-1] > now - 3600})
            events = self.entries[key]
            while events and events[0] <= now - seconds:
                events.popleft()
            if len(events) >= limit:
                raise ApiError(429, "RATE_LIMITED", "请求过于频繁，请稍后重试")
            events.append(now)


limiter = Limiter()


def check_origin(request: Request):
    origin = request.headers.get("origin")
    if origin and origin not in settings().allowed_origins.split(","):
        raise ApiError(403, "ORIGIN_DENIED")


def require_admin(request: Request, db=Depends(get_db)):
    session = db.get(LoginSession, digest(request.cookies.get("opsark_session", "")))
    if not session or session.expires <= time.time():
        raise ApiError(401, "SESSION_EXPIRED", "请登录管理员账户")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
            raise ApiError(403, "CSRF_DENIED")
    return session

