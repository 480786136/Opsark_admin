import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_accounts import CREDS, configure, signup
from test_gateway import mock
from test_platform import auth
from test_platform import env as source_env

from app.cloud_models import GithubFlow, GithubIdentity
from app.config import settings
from app.db import get_db
from app.github_auth import challenge
from app.log_privacy import OAuthQueryFilter
from app.main import app
from app.security import token
from app.user_models import CreditLedger, UserAccount

env = source_env
PREFIX = "/api/core/v1/auth/github"


@pytest.fixture
def oauth(env, monkeypatch):
    monkeypatch.setattr(settings(), "model_key_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings(), "github_client_id", "synthetic-id")
    monkeypatch.setattr(settings(), "github_client_secret", "synthetic-secret")
    monkeypatch.setattr(settings(), "github_callback_url", "https://platform.example.test" + PREFIX + "/callback")
    configure(env, auth(env), tokens=321)
    return env


def start(c, mode="login", headers=None):
    verifier = token()
    r = c.post(PREFIX + "/start", json={"challenge": challenge(verifier), "mode": mode}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    query = parse_qs(urlsplit(body["authorize_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["read:user user:email"]
    assert "synthetic-secret" not in r.text and verifier not in r.text
    return {"flow_id": body["flow_id"], "verifier": verifier}, query["state"][0], query["code_challenge"][0]


def transport(*, email="github-user@example.test", github_id=123, verified=True, malformed=False):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "synthetic-github-access"})
        assert request.headers["authorization"] == "Bearer synthetic-github-access"
        if request.url.path == "/user":
            return httpx.Response(200, json=[] if malformed else {"id": github_id, "login": "synthetic-user"})
        assert request.url.path == "/user/emails"
        return httpx.Response(200, json=[{"email": email, "primary": True, "verified": verified}])

    mock(handler)
    return requests


def callback(c, state):
    r = c.get(PREFIX + "/callback", params={"state": state, "code": "synthetic-authorization-code"})
    assert r.status_code == 200, r.text
    assert "ouc_" not in r.text and "synthetic-authorization-code" not in r.text
    return r


def test_oauth_pkce_device_binding_one_use_and_single_registration_grant(oauth):
    c = oauth
    proof, state, upstream_challenge = start(c)
    assert c.post(PREFIX + "/complete", json=proof).json()["status"] == "pending"
    assert c.post(PREFIX + "/complete", json={**proof, "verifier": token()}).status_code == 401
    assert c.get(PREFIX + "/callback", params={"state": "unknown", "code": "synthetic"}).status_code == 400
    requests = transport()
    callback(c, state)
    exchanged = parse_qs(requests[0].content.decode())
    assert challenge(exchanged["code_verifier"][0]) == upstream_challenge
    assert exchanged["client_secret"] == ["synthetic-secret"]
    assert c.get(PREFIX + "/callback", params={"state": state, "code": "synthetic"}).status_code == 409
    result = c.post(PREFIX + "/complete", json=proof)
    assert result.status_code == 200, result.text
    assert result.json()["balance"]["available"] == 321
    assert result.json()["access_token"].startswith("ouc_")
    assert "synthetic-github-access" not in result.text
    assert c.post(PREFIX + "/complete", json=proof).status_code == 409
    proof2, state2, _ = start(c)
    callback(c, state2)
    assert c.post(PREFIX + "/complete", json=proof2).json()["user"]["id"] == result.json()["user"]["id"]
    for db in app.dependency_overrides[get_db]():
        assert db.scalar(select(func.count()).select_from(UserAccount)) == 1
        assert db.scalar(select(func.count()).select_from(CreditLedger)) == 1
        assert all(flow.encrypted_verifier is None for flow in db.scalars(select(GithubFlow)))


def test_same_email_does_not_auto_link_and_authenticated_link_grants_no_extra_credit(oauth):
    c = oauth
    local, headers = signup(c)
    proof, state, _ = start(c)
    transport(email=CREDS["email"])
    callback(c, state)
    failed = c.post(PREFIX + "/complete", json=proof)
    assert failed.status_code == 403 and failed.json()["error"]["code"] == "LOGIN_AND_LINK_REQUIRED"
    assert c.post(PREFIX + "/start", json={"challenge": challenge(token()), "mode": "link"}).status_code == 401
    proof2, state2, _ = start(c, "link", headers)
    callback(c, state2)
    assert c.post(PREFIX + "/complete", json=proof2).status_code == 401
    result = c.post(PREFIX + "/complete", json=proof2, headers=headers)
    assert result.json() == {"status": "linked"}
    for db in app.dependency_overrides[get_db]():
        assert db.scalar(select(GithubIdentity)).user_id == local["user"]["id"]
        assert db.scalar(select(func.count()).select_from(CreditLedger)) == 1


@pytest.mark.parametrize("kind", ["unverified", "malformed", "disabled"])
def test_oauth_failure_modes_never_create_user(oauth, kind):
    c = oauth
    proof, state, _ = start(c)
    if kind == "disabled":
        staff = auth(c)
        policy = c.get("/api/admin/v1/registration-policy").json()
        assert (
            c.put("/api/admin/v1/registration-policy", headers=staff, json={**policy, "enabled": False}).status_code
            == 200
        )
    transport(verified=kind != "unverified", malformed=kind == "malformed")
    callback(c, state)
    assert c.post(PREFIX + "/complete", json=proof).status_code == 403
    for db in app.dependency_overrides[get_db]():
        assert db.scalar(select(func.count()).select_from(UserAccount)) == 0
        assert db.get(GithubFlow, proof["flow_id"]).encrypted_verifier is None


def test_expired_flows_and_configuration_failure(oauth, monkeypatch):
    c = oauth
    proof, state, _ = start(c)
    for db in app.dependency_overrides[get_db]():
        db.get(GithubFlow, proof["flow_id"]).expires_at = time.time() - 1
        db.commit()
    assert c.post(PREFIX + "/complete", json=proof).status_code == 401
    assert c.get(PREFIX + "/callback", params={"state": state, "code": "synthetic"}).status_code == 400
    monkeypatch.setattr(settings(), "github_client_secret", "")
    assert c.get(PREFIX + "/config").json() == {"enabled": False}
    assert c.post(PREFIX + "/start", json={"challenge": challenge(token())}).status_code == 503


def test_default_access_log_excludes_callback_query():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", PREFIX + "/callback?code=synthetic&state=synthetic", "1.1", 200),
        None,
    )
    assert OAuthQueryFilter().filter(record)
    assert "code=" not in record.getMessage() and "state=" not in record.getMessage()
