import httpx
import pytest
from sqlalchemy import select
from test_accounts import configure, signup
from test_gateway import mock
from test_gateway import setup as source_setup
from test_platform import auth
from test_platform import env as source_env

from app.db import get_db
from app.main import app
from app.models import ModelCall
from app.user_models import AccountAudit, CreditAccount, UserAccount

env = source_env
setup = source_setup


def test_official_trace_is_correlated_to_authenticated_user_and_not_sent_upstream(setup):
    c, staff, *_ = setup
    configure(c, staff, models=["public-model"])
    account, bearer = signup(c)
    upstream = []

    def respond(request):
        upstream.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "synthetic answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    mock(respond)
    headers = {
        **bearer,
        "Idempotency-Key": "trace-request-0000001",
        "X-Opsark-Task-Id": "task-one",
        "X-Opsark-Round-Id": "round-one",
        "X-Opsark-Step-Id": "step-one",
        "X-Opsark-Phase-Index": "2",
        "X-Opsark-Operation": "review",
        "X-Opsark-Client-Request-Id": "local-request-1",
        "X-Opsark-User-Id": "forged-user",
    }
    r = c.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "public-model", "messages": [{"role": "user", "content": "synthetic private prompt"}]},
    )
    assert r.status_code == 200, r.text
    result = c.get("/api/admin/v1/call-monitor/requests", params={"user_id": account["user"]["id"]}).json()
    assert result["total"] == 1
    call = result["items"][0]
    assert call["user_id"] == account["user"]["id"]
    assert call["user_email"] == account["user"]["email"]
    assert (call["task_id"], call["step_id"], call["round_id"], call["operation"], call["phase_index"]) == (
        "task-one",
        "step-one",
        "round-one",
        "review",
        2,
    )
    assert call["client_request_id"] == "local-request-1"
    assert not any(h.startswith("x-opsark") for h in upstream[0].headers)
    assert b"task-one" not in upstream[0].content
    detail = c.get("/api/admin/v1/calls/" + call["id"]).json()
    assert detail["detail_policy"] == "redacted_snapshot"
    assert "synthetic private prompt" in detail["detail"]["input"]["text"]
    assert "synthetic answer" in detail["detail"]["output"]["text"]
    assert "synthetic private prompt" not in str(result)
    for db in app.dependency_overrides[get_db]():
        audit = db.scalar(select(AccountAudit).where(AccountAudit.action == "model_call_viewed"))
        assert audit.actor_id and audit.detail["call_id"] == call["id"]
        assert audit.detail["user_id"] == account["user"]["id"]
        assert audit.detail["snapshot_available"] is True
        assert "synthetic private prompt" not in str(audit.detail)


@pytest.mark.parametrize(
    "trace",
    [
        {"X-Opsark-Task-Id": "a" * 129},
        {"X-Opsark-Task-Id": "../escape"},
        {"X-Opsark-Operation": "invented"},
        {"X-Opsark-Phase-Index": "-1"},
        {"X-Opsark-Step-Id": "step-without-task"},
    ],
)
def test_trace_validation_never_dispatches_invalid_metadata(setup, trace):
    c, _, _, _, issued = setup
    mock(lambda _: pytest.fail("Invalid trace dispatched upstream"))
    response = c.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + issued["api_key"], **trace},
        json={"model": "public-model", "messages": [{"role": "user", "content": "test"}]},
    )
    assert response.status_code == 422
    assert c.get("/api/admin/v1/call-monitor/requests").json()["total"] == 0


def seed_calls():
    for db in app.dependency_overrides[get_db]():
        for uid in ("person-a", "person-b"):
            db.add(
                UserAccount(
                    id=uid, email=f"{uid}@example.test", password_hash="unused", disabled=False, created_at=1000
                )
            )
            db.flush()
            db.add(CreditAccount(user_id=uid, available=0, reserved=0))
        for i in range(25):
            db.add(
                ModelCall(
                    id=f"call-{i:02}",
                    key_id=f"session-{i % 2}",
                    owner="person-a",
                    user_id="person-a",
                    task_id="shared-task",
                    round_id="round-one",
                    step_id=f"step-{i}",
                    operation="review",
                    model="synthetic-model",
                    provider_id="test-provider",
                    started_at=1000 + i,
                    status="failed" if i == 24 else "succeeded",
                    input_tokens=10 if i != 24 else None,
                    output_tokens=5 if i != 24 else None,
                    duration_ms=100,
                )
            )
        db.add(
            ModelCall(
                id="other-user-call",
                key_id="session-other",
                owner="person-b",
                user_id="person-b",
                task_id="shared-task",
                model="synthetic-model",
                provider_id="test-provider",
                started_at=2000,
                status="running",
            )
        )
        db.add(
            ModelCall(
                id="legacy-key-call",
                key_id="legacy-key",
                owner="person-a",
                task_id="shared-task",
                model="synthetic-model",
                provider_id="test-provider",
                started_at=2001,
                status="succeeded",
            )
        )
        db.add(
            ModelCall(
                id="unlinked-call",
                key_id="session-2",
                owner="person-a",
                user_id="person-a",
                model="synthetic-model",
                provider_id="test-provider",
                started_at=2002,
                status="succeeded",
            )
        )
        db.commit()


def test_task_groups_never_merge_users_or_infer_key_owner_identity(env):
    auth(env)
    seed_calls()
    groups = env.get("/api/admin/v1/call-monitor/tasks").json()
    assert groups["total"] == 3
    data = {item["principal"]: item for item in groups["items"]}
    assert data["user:person-a"]["total"] == 25  # different login sessions share the user's task
    assert data["user:person-b"]["total"] == 1
    assert data["key:legacy-key"]["user_id"] is None
    url = "/api/admin/v1/call-monitor/requests"
    query = {"task_id": "shared-task", "principal_id": "user:person-a", "order": "asc"}
    first = env.get(url, params=query).json()
    second = env.get(url, params={**query, "page": 2}).json()
    assert first["total"] == 25
    assert len(second["items"]) == 5
    assert [item["id"] for item in first["items"] + second["items"]] == [f"call-{i:02}" for i in range(25)]
    assert first["summary"]["failed"] == 1
    assert first["summary"]["input_tokens"] == 240 and first["summary"]["unknown_usage"] == 1
    assert env.get(url, params={"association": "unlinked"}).json()["total"] == 1
    assert env.get(url, params={"user_id": "person-a"}).json()["total"] == 26


def test_request_search_filters_bounds_and_staff_auth(env):
    auth(env)
    seed_calls()
    url = "/api/admin/v1/call-monitor/requests"
    assert env.get(url, params={"q": "PERSON-B@EXAMPLE.TEST"}).json()["total"] == 1
    assert env.get(url, params={"q": "%"}).json()["total"] == 0
    assert env.get(url, params={"q": "call-24", "status": "failed"}).json()["total"] == 1
    assert env.get(url, params={"started_after": 1005, "started_before": 1010}).json()["total"] == 6
    assert env.get(url, params={"model": "missing"}).json()["total"] == 0
    for params in (
        {"page": 0},
        {"page_size": 101},
        {"status": "unknown"},
        {"started_after": 2, "started_before": 1},
        {"principal_id": "forged"},
    ):
        assert env.get(url, params=params).status_code == 422
    assert env.get("/api/admin/v1/calls/legacy-key-call").json()["call"]["identity_kind"] == "model_key"
    env.cookies.clear()
    assert env.get(url).status_code == 401
    assert env.get("/api/admin/v1/call-monitor/tasks").status_code == 401
