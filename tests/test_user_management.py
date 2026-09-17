"""Staff user administration; all identities, sessions and databases are synthetic."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_accounts import CREDS, configure, signup
from test_gateway import setup as source_setup
from test_platform import auth
from test_platform import env as source_env

from app.accounts import issue_session
from app.db import get_db
from app.main import app
from app.security import ApiError
from app.user_models import AccountAudit, CreditAccount, UserAccount, UserSession

env = source_env
setup = source_setup


def seed_users(count):
    for db in app.dependency_overrides[get_db]():
        for i in range(count):
            uid = f"synthetic-user-{i:03}"
            db.add(
                UserAccount(
                    id=uid,
                    email=f"person-{i:03}@example.test",
                    password_hash="unused",
                    disabled=i % 2 == 0,
                    created_at=1000,
                )
            )
            db.flush()
            db.add(CreditAccount(user_id=uid, available=i, reserved=0))
        db.commit()


def status_body(disabled):
    return {"disabled": disabled, "expected_disabled": not disabled, "reason": "Synthetic security review"}


def test_search_pages_literal_matching_and_safe_projection(env):
    auth(env)
    seed_users(205)
    url = "/api/admin/v1/users"
    first = env.get(url, params={"page_size": 100}).json()
    second = env.get(url, params={"page_size": 100, "page": 2}).json()
    third = env.get(url, params={"page_size": 100, "page": 3}).json()
    assert first["total"] == 205
    ids = [row["id"] for data in (first, second, third) for row in data["items"]]
    assert len(ids) == len(set(ids)) == 205
    assert ids == sorted(ids, reverse=True)  # deterministic tie-breaker
    assert set(first["items"][0]) == {"id", "email", "disabled", "created_at", "available", "reserved", "revision"}
    assert env.get(url, params={"q": "  PERSON-019@EXAMPLE.TEST "}).json()["total"] == 1
    assert env.get(url, params={"q": "synthetic-user-020"}).json()["items"][0]["disabled"]
    for term in ("%", "_", "' OR 1=1 --", "missing"):
        assert env.get(url, params={"q": term}).json()["total"] == 0
    assert env.get(url, params={"status": "disabled"}).json()["total"] == 103
    assert env.get(url, params={"status": "active"}).json()["total"] == 102
    assert env.get(url, params={"page": 99}).json()["items"] == []
    assert env.get(url + "/missing").status_code == 404


@pytest.mark.parametrize(
    "params", [{"page": 0}, {"page_size": 101}, {"page_size": 0}, {"status": "oops"}, {"q": "x" * 255}]
)
def test_user_query_validation(env, params):
    auth(env)
    assert env.get("/api/admin/v1/users", params=params).status_code == 422


def test_ban_unban_revokes_all_sessions_preserves_credit_and_cloud_gates(setup):
    c, staff, *_ = setup
    configure(c, staff, models=["public-model"], tokens=1234)
    account, bearer = signup(c)
    second = c.post("/api/core/v1/login", json=CREDS).json()
    url = f"/api/admin/v1/users/{account['user']['id']}"
    result = c.put(url + "/status", headers=staff, json=status_body(True))
    assert result.status_code == 200, result.text
    assert result.json()["revoked_count"] == 2
    assert result.json()["user"]["disabled"]
    for path in ("/api/core/v1/me", "/api/core/v1/usage", "/api/core/v1/skills", "/v1/models"):
        assert c.get(path, headers=bearer).status_code == 401, path
    assert (
        c.post(
            "/v1/chat/completions",
            headers=bearer,
            json={"model": "public-model", "messages": [{"role": "user", "content": "synthetic"}]},
        ).status_code
        == 401
    )
    assert c.post("/api/core/v1/login", json=CREDS).status_code == 401
    assert c.post("/api/core/v1/session/refresh", json={"refresh_token": second["refresh_token"]}).status_code == 401
    assert c.get("/api/core/v1/official-models").json()["models"]  # public discovery remains public
    assert c.put(url + "/status", headers=staff, json=status_body(True)).status_code == 409
    assert c.put(url + "/status", headers=staff, json=status_body(False)).status_code == 200
    assert c.get("/api/core/v1/me", headers=bearer).status_code == 401
    assert c.post("/api/core/v1/session/refresh", json={"refresh_token": account["refresh_token"]}).status_code == 401
    login = c.post("/api/core/v1/login", json=CREDS)
    assert login.status_code == 200
    assert login.json()["balance"]["available"] == 1234
    assert len(c.get(url + "/ledger").json()) == 1
    for db in app.dependency_overrides[get_db]():
        events = list(
            db.scalars(select(AccountAudit).where(AccountAudit.action.in_(["user_disabled", "user_enabled"])))
        )
        assert len(events) == 2
        assert all(e.actor_id and e.detail["user_id"] == account["user"]["id"] and e.detail["reason"] for e in events)


def test_session_list_single_revoke_all_revoke_and_identity_isolation(env):
    staff = auth(env)
    configure(env, staff)
    account, first = signup(env)
    other = env.post("/api/core/v1/login", json=CREDS).json()
    second = {"Authorization": "Bearer " + other["access_token"]}
    url = f"/api/admin/v1/users/{account['user']['id']}/sessions"
    result = env.get(url).json()
    assert result["total"] == result["revocable_count"] == 2
    assert set(result["items"][0]) == {"id", "status", "access_expires", "refresh_expires", "can_revoke"}
    target = result["items"][-1]["id"]
    action = {"reason": "Synthetic lost device"}
    seed_users(1)
    wrong = env.post(f"/api/admin/v1/users/synthetic-user-000/sessions/{target}/revoke", headers=staff, json=action)
    assert wrong.status_code == 404
    assert env.post(url + f"/{target}/revoke", headers=staff, json=action).json()["revoked_count"] == 1
    assert env.post(url + f"/{target}/revoke", headers=staff, json=action).json()["revoked_count"] == 0
    assert env.get("/api/core/v1/me", headers=first).status_code == 401
    assert env.get("/api/core/v1/me", headers=second).status_code == 200
    assert env.post("/api/core/v1/session/refresh", json={"refresh_token": account["refresh_token"]}).status_code == 401
    assert env.post(url + "/revoke-all", headers=staff, json=action).json()["revoked_count"] == 1
    assert env.get("/api/core/v1/me", headers=second).status_code == 401
    assert env.post(url + "/revoke-all", headers=staff, json=action).json()["revoked_count"] == 0
    assert env.get(url, params={"status": "active"}).json()["total"] == 0
    assert env.get(url, params={"status": "revoked"}).json()["total"] == 2
    assert not env.get(url.replace("/sessions", "")).json()["disabled"]
    assert env.post("/api/core/v1/login", json=CREDS).status_code == 200
    for db in app.dependency_overrides[get_db]():
        events = list(
            db.scalars(
                select(AccountAudit).where(AccountAudit.action.in_(["user_session_revoked", "user_sessions_revoked"]))
            )
        )
        assert len(events) == 2
        assert "access" not in str([e.detail for e in events])


def test_session_states_pagination_and_bounds(env):
    auth(env)
    seed_users(2)
    now = time.time()
    for db in app.dependency_overrides[get_db]():
        for i in range(23):
            db.add(
                UserSession(
                    id=f"session-{i:02}",
                    user_id="synthetic-user-001",
                    access_hash=f"access-{i}",
                    refresh_hash=f"refresh-{i}",
                    access_expires=now - 5 if i == 0 else now + 900,
                    refresh_expires=now - 5 if i == 1 else now + 9000,
                    revoked=i == 2,
                )
            )
        db.commit()
    url = "/api/admin/v1/users/synthetic-user-001/sessions"
    result = env.get(url, params={"page_size": 100}).json()
    states = {s["id"]: s["status"] for s in result["items"]}
    assert states["session-00"] == "refresh_required"
    assert states["session-01"] == "expired"
    assert states["session-02"] == "revoked"
    assert result["revocable_count"] == 21
    assert env.get(url, params={"page": 2}).json()["items"] == result["items"][20:]
    assert env.get(url, params={"status": "active"}).json()["total"] == 21
    assert env.get(url, params={"status": "expired"}).json()["total"] == 1
    for params in ({"page_size": 101}, {"page": 0}, {"status": "unknown"}):
        assert env.get(url, params=params).status_code == 422
    assert env.get("/api/admin/v1/users/missing/sessions").status_code == 404


def test_security_actions_require_staff_csrf_reason_and_existing_targets(env):
    staff = auth(env)
    configure(env, staff)
    account, bearer = signup(env)
    url = f"/api/admin/v1/users/{account['user']['id']}"
    sid = env.get(url + "/sessions").json()["items"][0]["id"]
    actions = [
        ("PUT", url + "/status", status_body(True)),
        ("POST", url + "/sessions/revoke-all", {"reason": "review"}),
        ("POST", url + f"/sessions/{sid}/revoke", {"reason": "review"}),
    ]
    for method, path, body in actions:
        assert env.request(method, path, json=body).status_code == 403
        assert (
            env.request(method, path, headers={**staff, "Origin": "https://evil.example"}, json=body).status_code == 403
        )
        assert env.request(method, path, headers=staff, json={**body, "reason": "  "}).status_code == 422
        assert (
            env.request(method, path.replace(account["user"]["id"], "missing"), headers=staff, json=body).status_code
            == 404
        )
    env.cookies.clear()
    for method, path, body in actions:
        assert env.request(method, path, headers=bearer, json=body).status_code == 401
    assert env.get(url + "/sessions", headers=bearer).status_code == 401


def test_stale_login_snapshot_cannot_issue_session_after_ban(env):
    staff = auth(env)
    configure(env, staff)
    account, _ = signup(env)
    stale = SimpleNamespace(**account["user"])
    assert env.put(f"/api/admin/v1/users/{stale.id}/status", headers=staff, json=status_body(True)).status_code == 200
    for db in app.dependency_overrides[get_db]():
        with pytest.raises(ApiError) as exc:
            issue_session(db, stale)
        assert exc.value.status == 401


def test_concurrent_refresh_and_ban_never_resurrect_session(env):
    staff = auth(env)
    configure(env, staff)
    account, bearer = signup(env)
    url = f"/api/admin/v1/users/{account['user']['id']}/status"
    barrier = Barrier(2)

    def refresh():
        barrier.wait(timeout=5)
        return env.post("/api/core/v1/session/refresh", json={"refresh_token": account["refresh_token"]})

    def ban():
        barrier.wait(timeout=5)
        return env.put(url, headers=staff, json=status_body(True))

    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshed = pool.submit(refresh)
        banned = pool.submit(ban)
        assert banned.result(timeout=10).status_code == 200
        response = refreshed.result(timeout=10)
    assert response.status_code in {200, 401}
    assert env.put(url, headers=staff, json=status_body(False)).status_code == 200
    assert env.get("/api/core/v1/me", headers=bearer).status_code == 401
    if response.status_code == 200:
        assert (
            env.get(
                "/api/core/v1/me", headers={"Authorization": "Bearer " + response.json()["access_token"]}
            ).status_code
            == 401
        )
        assert (
            env.post(
                "/api/core/v1/session/refresh", json={"refresh_token": response.json()["refresh_token"]}
            ).status_code
            == 401
        )


def test_admin_scope_changes_public_and_authenticated_catalogues_and_call_permission(setup):
    c, staff, *_ = setup
    configure(c, staff, models=["public-model"])
    _, bearer = signup(c)
    assert c.get("/api/core/v1/official-models").json()["models"][0]["id"] == "public-model"
    assert c.get("/v1/models", headers=bearer).json()["data"][0]["id"] == "public-model"
    configure(c, staff, models=[])
    assert c.get("/api/core/v1/official-models").json()["models"] == []
    assert c.get("/v1/models", headers=bearer).json()["data"] == []
    response = c.post(
        "/v1/chat/completions",
        headers={**bearer, "Idempotency-Key": "scope-denied-synthetic-01"},
        json={"model": "public-model", "messages": [{"role": "user", "content": "synthetic"}]},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MODEL_DENIED"
