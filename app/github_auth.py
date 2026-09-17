"""Server-mediated authorization-code + PKCE, with a separately bound desktop verifier."""

# ruff: noqa: B008
import base64
import hashlib
import secrets
import time
from typing import Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .accounts import Strict, auth_limit, hasher, issue_session, policy, require_user
from .cloud_common import seal, secure_url, unseal
from .cloud_models import GithubFlow, GithubIdentity
from .config import settings
from .credits import balance, change
from .db import get_db
from .security import ApiError, digest, limiter, token
from .user_models import CreditAccount, UserAccount

router = APIRouter(prefix="/api/core/v1/auth/github")


def configured():
    s = settings()
    return bool(s.github_client_id and s.github_client_secret and s.github_callback_url and s.model_key_encryption_key)


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class Start(Strict):
    challenge: str = Field(pattern=r"^[a-zA-Z0-9_-]{43}$")
    mode: Literal["login", "link"] = "login"


@router.get("/config")
def config():
    return {"enabled": configured()}


@router.post("/start")
def start(body: Start, request: Request, db=Depends(get_db)):
    auth_limit(request)
    if not configured():
        raise ApiError(503, "GITHUB_NOT_CONFIGURED", "管理员尚未配置 GitHub 登录")
    s = settings()
    secure_url(s.github_callback_url)
    user_id = require_user(request, db).user_id if body.mode == "link" else None
    raw_state, verifier, flow_id = token(), token(), token()
    db.add(
        GithubFlow(
            id=flow_id,
            state_hash=digest(raw_state),
            native_challenge=body.challenge,
            encrypted_verifier=seal(verifier),
            status="pending",
            mode=body.mode,
            user_id=user_id,
            expires_at=time.time() + 600,
        )
    )
    db.commit()
    return {
        "flow_id": flow_id,
        "expires_in": 600,
        "authorize_url": "https://github.com/login/oauth/authorize?"
        + urlencode(
            {
                "client_id": s.github_client_id,
                "redirect_uri": s.github_callback_url,
                "scope": "read:user user:email",
                "state": raw_state,
                "code_challenge": challenge(verifier),
                "code_challenge_method": "S256",
            }
        ),
    }


async def github_json(client, method, url, **kwargs):
    response = await client.request(method, url, timeout=20, **kwargs)
    if response.status_code != 200 or len(response.content) > 1024 * 1024:
        raise ApiError(502, "GITHUB_UNAVAILABLE")
    return response.json()


def resolve_identity(db, flow, github_id, email):
    linked = db.get(GithubIdentity, github_id)
    if flow.mode == "link":
        user = db.get(UserAccount, flow.user_id)
        if not user or user.disabled:
            raise ApiError(403, "ACCOUNT_DISABLED")
        if linked and linked.user_id != user.id:
            raise ApiError(409, "GITHUB_ALREADY_LINKED")
        existing = db.scalar(select(GithubIdentity).where(GithubIdentity.user_id == user.id))
        if existing and existing.github_id != github_id:
            raise ApiError(409, "ACCOUNT_ALREADY_LINKED")
        if not linked:
            db.add(GithubIdentity(github_id=github_id, user_id=user.id))
        return user
    if linked:
        user = db.get(UserAccount, linked.user_id)
        if not user or user.disabled:
            raise ApiError(403, "ACCOUNT_DISABLED")
        return user
    if db.scalar(select(UserAccount).where(UserAccount.email == email)):
        # A matching email is NOT proof of ownership of an existing OpsArk account.
        raise ApiError(409, "LOGIN_AND_LINK_REQUIRED")
    p = policy(db)
    if not p.enabled:
        raise ApiError(403, "REGISTRATION_DISABLED")
    user = UserAccount(
        email=email, password_hash=hasher.hash(token() + token()), disabled=False, created_at=time.time()
    )
    db.add(user)
    db.flush()
    db.add(CreditAccount(user_id=user.id, available=0, reserved=0))
    db.flush()
    change(
        db,
        user.id,
        p.initial_tokens,
        0,
        "initial_grant",
        "registration",
        "registration",
        f"GitHub 注册赠额，策略版本 {p.revision}",
    )
    db.add(GithubIdentity(github_id=github_id, user_id=user.id))
    return user


@router.get("/callback")
async def callback(request: Request, state: str = "", code: str = "", error: str = "", db=Depends(get_db)):
    if not configured() or not state or len(state) > 256 or len(code) > 1024:
        raise ApiError(400, "INVALID_OAUTH_CALLBACK")
    flow = db.scalar(select(GithubFlow).where(GithubFlow.state_hash == digest(state)))
    if not flow or flow.expires_at <= time.time():
        raise ApiError(400, "OAUTH_FLOW_EXPIRED")
    won = db.execute(
        update(GithubFlow).where(GithubFlow.id == flow.id, GithubFlow.status == "pending").values(status="exchanging")
    )
    if won.rowcount != 1:
        raise ApiError(409, "OAUTH_CALLBACK_USED")
    verifier = unseal(flow.encrypted_verifier)
    db.commit()
    try:
        if error or not code:
            raise ApiError(403, "GITHUB_ACCESS_DENIED")
        s = settings()
        result = await github_json(
            request.app.state.model_client,
            "POST",
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": s.github_client_id,
                "client_secret": s.github_client_secret,
                "code": code,
                "redirect_uri": s.github_callback_url,
                "code_verifier": verifier,
            },
        )
        access = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(access, str) or not access or len(access) > 4096 or "error" in result:
            raise ApiError(403, "GITHUB_ACCESS_DENIED")
        headers = {"Authorization": "Bearer " + access, "Accept": "application/vnd.github+json"}
        profile = await github_json(
            request.app.state.model_client, "GET", "https://api.github.com/user", headers=headers
        )
        emails = await github_json(
            request.app.state.model_client, "GET", "https://api.github.com/user/emails", headers=headers
        )
        if (
            not isinstance(profile, dict)
            or type(profile.get("id")) is not int
            or profile["id"] < 1
            or not isinstance(emails, list)
        ):
            raise ApiError(403, "GITHUB_IDENTITY_INVALID")
        verified = [
            e["email"].strip().lower()
            for e in emails
            if isinstance(e, dict)
            and e.get("verified") is True
            and e.get("primary") is True
            and isinstance(e.get("email"), str)
            and len(e["email"]) <= 254
            and "@" in e["email"]
        ]
        if not verified:
            raise ApiError(403, "GITHUB_VERIFIED_EMAIL_REQUIRED")
        user = resolve_identity(db, flow, str(profile["id"]), verified[0])
        flow.user_id = user.id
        flow.status = "authorized"
        flow.encrypted_verifier = None
        db.commit()
    except (ApiError, httpx.HTTPError, ValueError, IntegrityError) as failure:
        db.rollback()
        flow = db.get(GithubFlow, flow.id)
        flow.status = "failed"
        flow.encrypted_verifier = None
        flow.error = failure.code if isinstance(failure, ApiError) else "GITHUB_LOGIN_FAILED"
        db.commit()
    # No code, session, email or token is returned to the browser or placed in a redirect URL.
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>OpsArk</title><h1>请返回 OpsArk 客户端</h1><p>授权结果将在客户端显示。本页可以关闭。</p>",
        headers={
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
        },
    )


class Complete(Strict):
    flow_id: str = Field(min_length=20, max_length=64)
    verifier: str = Field(pattern=r"^[a-zA-Z0-9_-]{43,128}$")


@router.post("/complete")
def complete(body: Complete, request: Request, db=Depends(get_db)):
    limiter.check("github-poll:" + digest(body.flow_id), 20)
    flow = db.get(GithubFlow, body.flow_id)
    if (
        not flow
        or not secrets.compare_digest(flow.native_challenge, challenge(body.verifier))
        or flow.expires_at <= time.time()
    ):
        raise ApiError(401, "OAUTH_PROOF_INVALID", "授权已过期或设备校验失败，请重新发起")
    if flow.status in {"pending", "exchanging"}:
        return {"status": "pending"}
    if flow.status == "failed":
        explanations = {
            "LOGIN_AND_LINK_REQUIRED": "邮箱已有 OpsArk 账号，请先用密码登录，再绑定 GitHub",
            "REGISTRATION_DISABLED": "暂未开放新用户注册",
            "GITHUB_VERIFIED_EMAIL_REQUIRED": "请先在 GitHub 验证主邮箱",
        }
        raise ApiError(
            403, flow.error or "GITHUB_LOGIN_FAILED", explanations.get(flow.error, "GitHub 登录未完成，请重新发起授权")
        )
    user = db.get(UserAccount, flow.user_id)
    if not user or user.disabled:
        raise ApiError(403, "ACCOUNT_DISABLED")
    won = db.execute(
        update(GithubFlow).where(GithubFlow.id == flow.id, GithubFlow.status == "authorized").values(status="consumed")
    )
    if won.rowcount != 1:
        raise ApiError(409, "OAUTH_FLOW_CONSUMED", "该授权已使用，请重新登录")
    if flow.mode == "link":
        # Re-validate the current OpsArk session as well as the desktop proof.
        if require_user(request, db).user_id != user.id:
            raise ApiError(403, "ACCOUNT_CHANGED")
        db.commit()
        return {"status": "linked"}
    response = issue_session(db, user)
    db.commit()
    return {"status": "authenticated", **response, "balance": balance(db, user.id)}
