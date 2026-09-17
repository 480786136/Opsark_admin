import copy
import hashlib
import json

import pytest
from test_platform import auth
from test_platform import env as source_env

env = source_env
ADMIN = "/api/admin/v1/official-content"
PUBLIC = "/api/core/v1/official-content"
INFO = "/api/core/v1/client-info?platform=macos&arch=aarch64&content_protocol=2&version="


def save(c, headers, kind="skills", edit=None):
    draft = c.get(f"{ADMIN}/{kind}").json()
    items = copy.deepcopy(draft["items"])
    if edit:
        edit(items)
    response = c.put(f"{ADMIN}/{kind}", headers=headers, json={"revision": draft["revision"], "items": items})
    assert response.status_code == 200, response.text
    return response.json()


def publish(c, headers, kind="skills", minimum="0.3.0"):
    draft = c.get(f"{ADMIN}/{kind}").json()
    response = c.post(
        f"{ADMIN}/{kind}/releases",
        headers=headers,
        json={
            "revision": draft["revision"],
            "version": draft["next_version"],
            "min_core_version": minimum,
            "notes": "Synthetic release",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_drafts_are_private_and_unpublished_content_is_not_distributed(env):
    assert env.get(f"{ADMIN}/skills").status_code == 401
    assert env.get(INFO + "0.3.0").json()["system_skills"] is None
    headers = auth(env)
    draft = save(env, headers)
    assert draft["items"] and draft["revision"] == 1
    assert env.get(INFO + "0.3.0").json()["system_skills"] is None
    assert env.put(f"{ADMIN}/skills", json={"revision": 1, "items": draft["items"]}).status_code == 403
    assert env.put(f"{ADMIN}/skills", headers=headers, json={"revision": 0, "items": draft["items"]}).status_code == 409


def test_publication_is_immutable_hashed_and_compatible_without_login(env):
    headers = auth(env)
    save(env, headers)
    release = publish(env, headers)
    old = env.get(f"{ADMIN}/skills").json()
    response = env.post(
        f"{ADMIN}/skills/releases",
        headers=headers,
        json={
            "revision": old["revision"],
            "version": 1,
            "min_core_version": "0.3.0",
            "notes": "replace",
        },
    )
    assert response.status_code == 409
    env.cookies.clear()
    info = env.get(INFO + "0.3.0").json()
    assert info["system_skills"]["id"] == release["id"] and info["tools"] is None
    assert env.get(INFO + "0.2.0").json()["system_skills"] is None
    downloaded = env.get(release["download_path"]).json()
    assert hashlib.sha256(downloaded["content"].encode()).hexdigest() == release["sha256"]
    body = json.loads(downloaded["content"])
    assert body["version"] == 1 and body["required_tools"]
    assert all("planningContract" not in s and "builtIn" not in s for s in body["items"])
    assert env.get(f"{PUBLIC}/tools/{release['id']}").status_code == 404


def test_edit_publish_withdraw_and_restore_as_higher_version(env):
    headers = auth(env)
    first_draft = save(env, headers)
    first = publish(env, headers)
    save(env, headers, edit=lambda items: items[0].update(instructions="Inspect carefully before any changes."))
    second = publish(env, headers)
    assert second["version"] == 2
    payload = json.loads(env.get(second["download_path"]).json()["content"])
    assert payload["items"][0]["version"] == first_draft["items"][0]["version"] + 1
    assert env.delete(f"{ADMIN}/skills/releases/{second['id']}", headers=headers).status_code == 200
    assert env.get(second["download_path"]).status_code == 410
    assert env.get(INFO + "0.3.0").json()["system_skills"]["id"] == first["id"]
    historical = env.get(f"{ADMIN}/skills/releases/{first['id']}").json()
    save(env, headers, edit=lambda items: items.__setitem__(slice(None), json.loads(historical["content"])["items"]))
    restored = publish(env, headers)
    assert restored["version"] == 3
    assert (
        json.loads(env.get(restored["download_path"]).json()["content"])["items"][0]["instructions"]
        == first_draft["items"][0]["instructions"]
    )
    # A newer release requiring a newer binary does not hide the compatible publication.
    publish(env, headers, minimum="0.4.0")
    assert env.get(INFO + "0.3.0").json()["system_skills"]["version"] == 3
    assert env.get(INFO + "0.4.0").json()["system_skills"]["version"] == 4


def test_tool_publication_contains_configuration_but_no_execution_code(env):
    headers = auth(env)
    save(env, headers, "tools", lambda items: items[0].update(enabled=False))
    release = publish(env, headers, "tools")
    body = json.loads(env.get(release["download_path"]).json()["content"])
    assert body["items"][0]["enabled"] is False
    assert body["schema_version"] == 2
    assert all(
        set(t)
        == {
            "id",
            "enabled",
            "min_implementation_version",
            "name",
            "description",
            "usageInstructions",
            "outputDescription",
            "inputSchema",
        }
        for t in body["items"]
    )
    assert env.get(INFO + "0.3.0").json()["tools"]["version"] == 1
    # Older Core binaries can only decode the original switch-only format.
    assert env.get(INFO.replace("content_protocol=2&", "") + "0.3.0").json()["tools"] is None


def file_tool(items):
    return next(t for t in items if t["id"] == "files.read_content")


def test_tool_parameters_survive_save_reload_publication_and_history_copy(env):
    headers = auth(env)

    def edit(items):
        tool = file_tool(items)
        tool["name"] = "小文件读取"
        tool["inputSchema"]["properties"]["maxBytes"].update(default=4096, maximum=16384, description="最多读取 16 KiB")
        tool["inputSchema"]["properties"]["path"].update(minLength=1, maxLength=1024)

    saved = save(env, headers, "tools", edit)
    assert file_tool(saved["items"])["name"] == "小文件读取"
    assert file_tool(saved["items"])["inputSchema"]["properties"]["maxBytes"]["default"] == 4096
    assert env.get(f"{ADMIN}/tools").json()["items"] == saved["items"]
    first = publish(env, headers, "tools")
    downloaded = env.get(first["download_path"]).json()
    assert hashlib.sha256(downloaded["content"].encode()).hexdigest() == first["sha256"]
    assert json.loads(downloaded["content"])["items"] == saved["items"]
    save(
        env,
        headers,
        "tools",
        lambda items: file_tool(items)["inputSchema"]["properties"]["maxBytes"].update(default=8192),
    )
    publish(env, headers, "tools")
    historical = env.get(f"{ADMIN}/tools/releases/{first['id']}").json()
    save(
        env, headers, "tools", lambda items: items.__setitem__(slice(None), json.loads(historical["content"])["items"])
    )
    restored = publish(env, headers, "tools")
    assert restored["version"] == 3
    assert json.loads(env.get(restored["download_path"]).json()["content"])["items"] == saved["items"]


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s["properties"].update(newParameter={"type": "string"}),
        lambda s: s["properties"].pop("path"),
        lambda s: s["properties"]["maxBytes"].update(type="string"),
        lambda s: s["properties"]["maxBytes"].update(maximum=262145),
        lambda s: s["properties"]["maxBytes"].pop("maximum"),
        lambda s: s["properties"]["maxBytes"].update(default=0),
        lambda s: s["properties"]["maxBytes"].update(default=False),
        lambda s: s["properties"]["maxBytes"].update(enum=[1024, 4096]),
        lambda s: s["properties"]["path"].update(minLength=-1),
        lambda s: s["properties"]["path"].update(minLength=10, maxLength=5),
        lambda s: s.update(required=[]),
        lambda s: s.update(required=["path", "unknown"]),
        lambda s: s.update(additionalProperties=True),
        lambda s: s.update(executor="remote-code"),
    ],
)
def test_invalid_tool_parameters_do_not_change_saved_draft(env, change):
    headers = auth(env)
    draft = save(env, headers, "tools")
    items = copy.deepcopy(draft["items"])
    change(file_tool(items)["inputSchema"])
    result = env.put(f"{ADMIN}/tools", headers=headers, json={"revision": draft["revision"], "items": items})
    assert result.status_code == 422, result.text
    assert result.json()["error"]["code"] == "INVALID_TOOL_PARAMETERS"
    assert env.get(f"{ADMIN}/tools").json()["items"] == draft["items"]


def test_tool_nested_constraints_and_legacy_drafts(env):
    headers = auth(env)

    def edit(items):
        schema = next(t for t in items if t["id"] == "user.request_input")["inputSchema"]
        schema["required"].append("description")
        schema["properties"]["fields"]["maxItems"] = 3
        nested = schema["properties"]["fields"]["items"]["properties"]
        nested["type"]["enum"] = ["text", "password"]
        nested["required"]["default"] = True

    save(env, headers, "tools", edit)
    assert publish(env, headers, "tools")["schema_version"] == 2
    # Legacy three-field API inputs receive the full compiled defaults when saved.
    saved = save(
        env,
        headers,
        "tools",
        lambda items: items.__setitem__(
            slice(None), [{k: t[k] for k in ("id", "enabled", "min_implementation_version")} for t in items]
        ),
    )
    assert file_tool(saved["items"])["inputSchema"]["properties"]["maxBytes"]["default"] == 65536


@pytest.mark.parametrize(
    "kind, change",
    [
        ("skills", lambda items: items[0].update(planningContract={})),
        ("skills", lambda items: items[0].update(id="skill-personal")),
        ("skills", lambda items: items[0].update(allowedToolIds=["unknown.tool"])),
        ("skills", lambda items: items[0].update(instructions="Bearer " + "x" * 30)),
        ("tools", lambda items: items[0].update(implementation="replacement")),
        ("tools", lambda items: items[0].update(inputSchema=None)),
        ("tools", lambda items: items[0].update(description=None)),
        ("tools", lambda items: items[0].update(min_implementation_version=1000)),
        ("tools", lambda items: items.pop()),
        ("tools", lambda items: items[0].update(enabled="false")),
    ],
)
def test_invalid_content_cannot_enter_official_draft(env, kind, change):
    headers = auth(env)
    draft = env.get(f"{ADMIN}/{kind}").json()
    change(draft["items"])
    result = env.put(f"{ADMIN}/{kind}", headers=headers, json={"revision": 0, "items": draft["items"]})
    assert result.status_code == 422, result.text
    assert env.get(f"{ADMIN}/{kind}").json()["revision"] == 0


def test_stale_publication_cannot_publish_another_admins_draft(env):
    headers = auth(env)
    stale = save(env, headers)
    save(env, headers, edit=lambda items: items[0].update(name="Another administrator"))
    response = env.post(
        f"{ADMIN}/skills/releases",
        headers=headers,
        json={
            "revision": stale["revision"],
            "version": 1,
            "min_core_version": "0.3.0",
            "notes": "stale",
        },
    )
    assert response.status_code == 409
    assert env.get(INFO + "0.3.0").json()["system_skills"] is None


def test_updated_core_catalog_preserves_switches_and_requires_enabling_new_tools(env, monkeypatch):
    from app import official_content

    headers = auth(env)
    saved = save(env, headers, "tools", lambda items: items[0].update(enabled=False, name="管理员定义的名称"))
    updated = copy.deepcopy(official_content.catalog())
    updated["core_version"] = "0.4.0"
    updated["tools"][0]["version"] += 1
    updated["tools"].append({**updated["tools"][0], "id": "new.tool", "enabled": True})
    monkeypatch.setattr(official_content, "catalog", lambda: updated)
    draft = env.get(f"{ADMIN}/tools").json()
    assert draft["items"][0]["enabled"] is False
    assert draft["items"][0]["name"] == "管理员定义的名称"
    assert draft["items"][0]["min_implementation_version"] == saved["items"][0]["min_implementation_version"] + 1
    assert draft["items"][-1]["id"] == "new.tool" and draft["items"][-1]["enabled"] is False
    save(env, headers, "tools", lambda items: items[-1].update(enabled=True))
    release = publish(env, headers, "tools", minimum="0.4.0")
    assert env.get(INFO + "0.3.0").json()["tools"] is None
    assert env.get(INFO + "0.4.0").json()["tools"]["id"] == release["id"]


def test_old_clients_keep_their_latest_supported_tool_release(env):
    from app.cloud_models import OfficialContentRelease
    from app.db import get_db
    from app.main import app

    headers = auth(env)
    draft = save(env, headers, "tools")
    content = json.dumps(
        {
            "schema_version": 1,
            "kind": "tools",
            "version": 1,
            "min_core_version": "0.3.0",
            "required_tools": [],
            "items": [{k: t[k] for k in ("id", "enabled", "min_implementation_version")} for t in draft["items"]],
        }
    )
    for db in app.dependency_overrides[get_db]():
        db.add(
            OfficialContentRelease(
                id="legacy",
                kind="tools",
                version=1,
                min_core_version="0.3.0",
                notes="Existing switch-only publication",
                payload=content,
                sha256=hashlib.sha256(content.encode()).hexdigest(),
                created_at=0,
            )
        )
        db.commit()
    modern = publish(env, headers, "tools")
    assert modern["version"] == 2 and modern["schema_version"] == 2
    assert env.get(INFO + "0.3.0").json()["tools"]["id"] == modern["id"]
    assert env.get(INFO.replace("content_protocol=2&", "") + "0.3.0").json()["tools"]["id"] == "legacy"
    assert env.get(f"{PUBLIC}/tools/legacy").json()["content"] == content


def test_catalog_schema_changes_are_explained_before_resaving(env, monkeypatch):
    from app import official_content

    headers = auth(env)
    save(
        env,
        headers,
        "tools",
        lambda items: file_tool(items)["inputSchema"]["properties"]["maxBytes"].update(default=4096),
    )
    updated = copy.deepcopy(official_content.catalog())
    file = file_tool(updated["tools"])
    file["version"] += 1
    file["inputSchema"]["properties"]["maxBytes"].update(minimum=8192)
    monkeypatch.setattr(official_content, "catalog", lambda: updated)
    draft = env.get(f"{ADMIN}/tools").json()
    assert len(draft["catalog_warnings"]) == 1 and "files.read_content" in draft["catalog_warnings"][0]
    assert file_tool(draft["items"])["inputSchema"] == file["inputSchema"]
    saved = save(env, headers, "tools")
    assert saved["catalog_warnings"] == []
