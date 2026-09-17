import json
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from test_accounts import configure, signup
from test_gateway import setup as gateway_setup
from test_platform import auth
from test_platform import env as source_env

from app.cloud_models import CloudMutation, SupportTicket, UserSkill
from app.config import settings
from app.db import get_db
from app.main import app
from app.user_models import AccountAudit

env = source_env
setup = gateway_setup
PREFIX = "/api/core/v1"
SKILL = {
    "name": "Personal workflow",
    "category": "other",
    "description": "Only prose",
    "instructions": "Inspect the chosen project before proposing edits.",
    "matchRules": ["project"],
    "allowedToolIds": ["file.read"],
    "allowShell": False,
    "enabled": True,
    "version": 1,
}


@pytest.fixture
def cloud(env, monkeypatch):
    monkeypatch.setattr(settings(), "model_key_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings(), "release_allowed_hosts", "downloads.example.test")
    staff = auth(env)
    configure(env, staff)
    first, a = signup(env)
    second = env.post(PREFIX + "/register", json={"email": "second@example.test", "password": "synthetic-password-456"})
    assert second.status_code == 201
    return env, staff, first, a, {"Authorization": "Bearer " + second.json()["access_token"]}


def put_skill(c, headers, *, revision=0, content=SKILL, mutation=None, ident="skill-test"):
    return c.put(
        PREFIX + "/skills/" + ident,
        headers=headers,
        json={"base_revision": revision, "mutation_id": mutation or str(uuid.uuid4()), "content": content},
    )


def feedback_body(**extra):
    return {
        "mutation_id": str(uuid.uuid4()),
        "title": "Task could not finish",
        "message": "Synthetic diagnostic",
        "category": "task",
        "core_version": "0.3.0",
        "consent": True,
        "diagnostic": {
            "taskId": "task-test",
            "status": "failed",
            "stepCount": 2,
            "completedSteps": 1,
            "failedSteps": 1,
        },
        "log_excerpt": "operation=review; result=failed",
        **extra,
    }


def test_skill_owner_scope_encryption_and_idempotency(cloud):
    c, _, first, a, b = cloud
    mutation = str(uuid.uuid4())
    saved = put_skill(c, a, mutation=mutation)
    assert saved.status_code == 200, saved.text
    assert put_skill(c, a, mutation=mutation).json() == saved.json()
    assert put_skill(c, a, mutation=mutation, content={**SKILL, "name": "Changed"}).status_code == 409
    assert c.get(PREFIX + "/skills", headers=b).json()["items"] == []
    assert put_skill(c, b, content={**SKILL, "name": "Other owner"}).status_code == 200
    assert c.get(PREFIX + "/skills", headers=a).json()["items"][0]["content"] == SKILL
    for db in app.dependency_overrides[get_db]():
        row = db.get(UserSkill, (first["user"]["id"], "skill-test"))
        assert SKILL["instructions"] not in row.encrypted_payload
        assert SKILL["instructions"] not in json.dumps(db.scalar(select(CloudMutation)).response)
    assert c.get(PREFIX + "/skills").status_code == 401  # Admin cookie is not a user session.


def test_skill_conflicts_tombstones_and_explicit_restore(cloud):
    c, _, _, a, _ = cloud
    assert put_skill(c, a).status_code == 200
    assert put_skill(c, a, content={**SKILL, "name": "stale"}).status_code == 409
    assert put_skill(c, a, revision=1, content=None).json()["revision"] == 2
    deleted = c.get(PREFIX + "/skills", headers=a).json()["items"][0]
    assert deleted["deleted"] and deleted["content"] is None
    assert put_skill(c, a, revision=1).status_code == 409
    assert put_skill(c, a, revision=2).json()["revision"] == 3
    assert put_skill(c, a, ident="skill-missing", content=None).status_code == 404


@pytest.mark.parametrize(
    "extra",
    [
        {"builtIn": True},
        {"source": "system"},
        {"planningContract": {}},
        {"executor": "shell"},
        {"instructions": "Bearer " + "z" * 30},
    ],
)
def test_skill_rejects_system_fields_executables_and_credentials(cloud, extra):
    c, _, _, a, _ = cloud
    assert put_skill(c, a, content={**SKILL, **extra}).status_code == 422
    assert put_skill(c, a, ident="system-ssh").status_code == 422
    assert c.get(PREFIX + "/skills", headers=a).json()["items"] == []


def test_skill_cursor_pages_are_bounded(cloud):
    c, _, _, a, _ = cloud
    for i in range(22):
        assert put_skill(c, a, ident=f"skill-{i:03}").status_code == 200
    page = c.get(PREFIX + "/skills", headers=a).json()
    assert len(page["items"]) == 20
    last = c.get(PREFIX + "/skills", headers=a, params={"after": page["next_cursor"]}).json()
    assert len(last["items"]) == 2 and last["next_cursor"] is None
    assert c.get(PREFIX + "/skills", headers=a, params={"after": "../system"}).status_code == 422


def test_feedback_consent_ownership_replies_and_deletion(cloud):
    c, staff, _, a, b = cloud
    payload = feedback_body()
    missing = dict(payload)
    missing.pop("consent")
    assert c.post(PREFIX + "/feedback", headers=a, json=missing).status_code == 422
    assert c.post(PREFIX + "/feedback", json=payload).status_code == 201
    response = c.post(PREFIX + "/feedback", headers=a, json=payload)
    assert response.status_code == 201, response.text
    assert c.post(PREFIX + "/feedback", headers=a, json=payload).json() == response.json()
    ident = response.json()["id"]
    url = PREFIX + "/feedback/" + ident
    assert c.get(url, headers=b).status_code == 404
    assert c.delete(url, headers=b).status_code == 404
    assert c.get(PREFIX + "/feedback", headers=b).json() == []
    assert "content" not in c.get(PREFIX + "/feedback", headers=a).json()[0]
    staff_url = "/api/admin/v1/feedback/" + ident
    assert c.get(staff_url).json()["content"]["log_excerpt"] == payload["log_excerpt"]
    reply = {"revision": 1, "status": "resolved", "message": "Please retry after reconnecting."}
    assert c.put(staff_url, json=reply).status_code == 403
    assert c.put(staff_url, headers=staff, json=reply).status_code == 200
    assert c.put(staff_url, headers=staff, json=reply).status_code == 409
    assert c.get(url, headers=a).json()["content"]["replies"][0]["message"] == reply["message"]
    for db in app.dependency_overrides[get_db]():
        row = db.get(SupportTicket, ident)
        assert payload["message"] not in row.encrypted_payload
        assert reply["message"] not in row.encrypted_payload
        assert payload["log_excerpt"] not in json.dumps([r.detail for r in db.scalars(select(AccountAudit))])
    assert c.delete(url, headers=a).status_code == 200
    assert c.get(staff_url).json()["content"] is None
    assert c.put(staff_url, headers=staff, json={**reply, "revision": 3}).status_code == 410


@pytest.mark.parametrize(
    "extra",
    [
        {"consent": False},
        {"category": "general"},
        {"diagnostic": None},
        {"log_excerpt": "sk-" + "a" * 30},
        {"message": "-----BEGIN PRIVATE KEY-----"},
        {"attachments": ["/tmp/private"]},
        {"log_excerpt": "x" * 16001},
    ],
)
def test_feedback_rejects_unapproved_or_secret_payloads(cloud, extra):
    c, _, _, a, _ = cloud
    assert c.post(PREFIX + "/feedback", headers=a, json=feedback_body(**extra)).status_code == 422
    assert c.get(PREFIX + "/feedback", headers=a).json() == []


def test_feedback_retention_clears_content_but_keeps_minimal_metadata(cloud):
    c, _, _, a, _ = cloud
    ident = c.post(PREFIX + "/feedback", headers=a, json=feedback_body()).json()["id"]
    for db in app.dependency_overrides[get_db]():
        db.get(SupportTicket, ident).expires_at = time.time() - 1
        db.commit()
    assert c.get(PREFIX + "/feedback/" + ident, headers=a).json()["content"] is None
    for db in app.dependency_overrides[get_db]():
        assert db.get(SupportTicket, ident).encrypted_payload is None
        assert db.get(SupportTicket, ident).status == "expired"


def test_release_guidance_platform_semver_whitelist_withdrawal_and_cloud_gate(cloud):
    c, staff, _, a, _ = cloud
    policy = {
        "revision": 0,
        "support_email": "support@example.test",
        "support_url": "https://support.example.test",
        "min_cloud_version": "0.3.1",
    }
    assert c.put("/api/admin/v1/client-policy", headers=staff, json=policy).status_code == 200
    assert c.put("/api/admin/v1/client-policy", headers=staff, json=policy).status_code == 409
    release = {
        "platform": "macos",
        "arch": "aarch64",
        "version": "0.3.10",
        "notes": "Update notes",
        "download_url": "https://downloads.example.test/core.dmg",
        "sha256": "a" * 64,
    }
    assert (
        c.post(
            "/api/admin/v1/client-releases", headers=staff, json={**release, "download_url": "https://evil.test/file"}
        ).status_code
        == 422
    )
    r = c.post("/api/admin/v1/client-releases", headers=staff, json=release)
    assert r.status_code == 201, r.text
    assert c.post("/api/admin/v1/client-releases", headers=staff, json=release).status_code == 409
    info = c.get(PREFIX + "/client-info", params={"platform": "macos", "arch": "aarch64", "version": "0.3.9"}).json()
    assert info["latest"]["version"] == "0.3.10" and not info["update_required"]
    old = c.get(PREFIX + "/client-info", params={"platform": "windows", "arch": "x86_64", "version": "0.3.0"}).json()
    assert old["latest"] is None and old["update_required"]
    assert c.get(PREFIX + "/skills", headers={**a, "X-Opsark-Version": "0.3.0"}).status_code == 426
    assert c.get(PREFIX + "/skills", headers={**a, "X-Opsark-Version": "0.3.1"}).status_code == 200
    assert c.get(PREFIX + "/me", headers=a).status_code == 200
    assert c.get(PREFIX + "/feedback", headers=a).status_code == 200  # Help still works for outdated Core.
    assert c.delete("/api/admin/v1/client-releases/" + r.json()["id"], headers=staff).status_code == 200
    assert (
        c.get(PREFIX + "/client-info", params={"platform": "macos", "arch": "aarch64", "version": "0.3.0"}).json()[
            "latest"
        ]
        is None
    )


def test_official_model_list_only_projects_public_name(setup):
    c, staff, pid, rid, _ = setup
    configure(c, staff, models=["public-model"])
    _, headers = signup(c)
    payload = {
        "alias": "public-model",
        "display_name": "官方体验模型",
        "provider_id": pid,
        "upstream_model": "private-model",
    }
    assert c.put("/api/admin/v1/routes/" + rid, headers=staff, json=payload).status_code == 200
    response = c.get("/v1/models", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"][0]["name"] == "官方体验模型"
    assert "private-model" not in response.text and "base_url" not in response.text and "api_key" not in response.text


PNG_IMAGE = {"name": "screenshot.png", "mime_type": "image/png", "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRZsAAAAASUVORK5CYII="}


def test_anonymous_feedback_images_full_logs_idempotency_and_private_access(cloud):
    c, _, _, a, _ = cloud
    payload = feedback_body(contact="user@example.test", images=[PNG_IMAGE] * 3, task_logs="full task log\n" * 2000)
    result = c.post(PREFIX + "/feedback", json=payload)
    assert result.status_code == 201, result.text
    assert c.post(PREFIX + "/feedback", json=payload).json() == result.json()
    assert c.post(PREFIX + "/feedback", json={**payload, "contact": "changed"}).status_code == 409
    ident = result.json()["id"]
    assert c.get(PREFIX + "/feedback/" + ident).status_code == 401
    assert c.get(PREFIX + "/feedback/" + ident, headers=a).status_code == 404
    assert c.delete(PREFIX + "/feedback/" + ident, headers=a).status_code == 404
    content = c.get("/api/admin/v1/feedback/" + ident).json()["content"]
    assert content["contact"] == payload["contact"]
    assert content["task_logs"] == payload["task_logs"]
    assert content["images"] == payload["images"]
    with next(app.dependency_overrides[get_db]()) as db:
        row = db.get(SupportTicket, ident)
        assert row.user_id is None
        assert payload["contact"] not in row.encrypted_payload
        assert PNG_IMAGE["data"] not in row.encrypted_payload
        row.expires_at = time.time() - 1
        db.commit()
    assert c.get("/api/admin/v1/feedback/" + ident).json()["content"] is None


@pytest.mark.parametrize("extra", [
    {"images": [PNG_IMAGE] * 4},
    {"images": [{**PNG_IMAGE, "mime_type": "image/svg+xml"}]},
    {"images": [{**PNG_IMAGE, "data": "bm90IGFuIGltYWdl"}]},
    {"images": [{**PNG_IMAGE, "data": "!invalid base64"}]},
    {"contact": "x" * 255},
    {"title": "   "},
    {"category": "general", "diagnostic": None, "log_excerpt": "", "task_logs": "unapproved logs"},
])
def test_public_feedback_rejects_invalid_attachments_and_content(cloud, extra):
    c, *_ = cloud
    assert c.post(PREFIX + "/feedback", json=feedback_body(**extra)).status_code == 422


def test_general_feedback_needs_neither_login_contact_images_nor_task(cloud):
    c, *_ = cloud
    result = c.post(PREFIX + "/feedback", json=feedback_body(category="general", diagnostic=None, log_excerpt=""))
    assert result.status_code == 201
    assert c.post(PREFIX + "/feedback", headers={"Authorization": "Bearer invalid"}, json=feedback_body()).status_code == 401


def test_default_contacts_are_public_and_changes_arrive_without_a_new_release(cloud):
    c, staff, *_ = cloud
    params = {"platform": "macos", "arch": "aarch64", "version": "0.3.0"}
    info = c.get(PREFIX + "/client-info", params=params).json()
    assert (info["support_wechat"], info["support_email"], info["developer_name"]) == ("zgkjkj", "zgkj@zgspace.cn", "智明")
    policy = c.get("/api/admin/v1/client-policy").json()
    policy.update(support_wechat="updated", support_email="updated@example.test", developer_name="开发者")
    assert c.put("/api/admin/v1/client-policy", headers=staff, json=policy).status_code == 200
    info = c.get(PREFIX + "/client-info", params=params).json()
    assert info["latest"] is None
    assert info["support_wechat"] == "updated" and info["developer_name"] == "开发者"
    assert info["support_email"] == "updated@example.test" and info["contact_revision"] == 1
