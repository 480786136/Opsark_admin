"""Official declarative content; tool implementations stay inside Core."""

# ruff: noqa: B008
import copy
import json
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .accounts import Strict
from .cloud_common import reject_credentials, version_tuple
from .cloud_models import OfficialContentDraft, OfficialContentRelease
from .db import get_db
from .security import ApiError, digest, require_admin
from .tool_configuration import validate_schema
from .user_models import AccountAudit

Kind = Literal["skills", "tools"]
admin = APIRouter(prefix="/api/admin/v1/official-content", dependencies=[Depends(require_admin)])
public = APIRouter(prefix="/api/core/v1/official-content")


@lru_cache
def catalog():
    return json.loads((Path(__file__).parent / "resources/core_catalog.json").read_text())


class Skill(Strict):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    name: str = Field(min_length=1, max_length=80)
    category: Literal[
        "connectivity", "source-control", "environment", "build", "data", "deployment", "transfer", "other"
    ]
    description: str = Field(min_length=1, max_length=1000)
    instructions: str = Field(min_length=1, max_length=8000)
    matchRules: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = Field(strict=True)
    version: int = Field(strict=True, ge=1, le=2147483647)
    allowShell: bool = Field(default=True, strict=True)
    allowedToolIds: list[str] = Field(default_factory=list, max_length=200)
    forbiddenToolIds: list[str] = Field(default_factory=list, max_length=200)


class ToolPolicy(Strict):
    id: str = Field(max_length=100)
    enabled: bool = Field(strict=True)
    min_implementation_version: int = Field(strict=True, ge=1, le=2147483647)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    usageInstructions: str | None = Field(default=None, min_length=1, max_length=2000)
    outputDescription: str | None = Field(default=None, min_length=1, max_length=1000)
    inputSchema: dict | None = None


TOOL_FIELDS = ("name", "description", "usageInstructions", "outputDescription", "inputSchema")


def validate_items(kind, items):
    tools = {item["id"]: item for item in catalog()["tools"]}
    if kind == "tools" and any(key in item and item[key] is None for item in items for key in TOOL_FIELDS):
        raise ApiError(422, "INVALID_OFFICIAL_CONTENT", "工具说明和参数协议不能为 null")
    try:
        result = [
            (Skill if kind == "skills" else ToolPolicy).model_validate(item).model_dump(exclude_none=True)
            for item in items
        ]
    except ValidationError:
        raise ApiError(422, "INVALID_OFFICIAL_CONTENT", "内容格式不正确；仅支持声明式 Skill 和工具配置")
    ids = [item["id"] for item in result]
    if not 1 <= len(ids) <= 200 or len(set(ids)) != len(ids):
        raise ApiError(422, "INVALID_OFFICIAL_CONTENT", "清单不能为空，且 ID 不能重复")
    for item in result:
        if kind == "skills":
            if item["id"].startswith("skill-") or any(
                not item[key].strip() for key in ("name", "description", "instructions")
            ):
                raise ApiError(422, "INVALID_OFFICIAL_CONTENT", "官方 Skill 不能使用个人 Skill 标识，正文不能为空")
            if not set(item["allowedToolIds"] + item["forbiddenToolIds"]).issubset(tools):
                raise ApiError(422, "UNKNOWN_TOOL", "Skill 引用了当前 Core 目录不存在的工具")
            if any(len(rule) > 500 for rule in item["matchRules"]):
                raise ApiError(422, "INVALID_OFFICIAL_CONTENT", "匹配规则过长")
        elif item["id"] not in tools or item["min_implementation_version"] != tools[item["id"]]["version"]:
            raise ApiError(422, "UNKNOWN_TOOL", "工具实现版本由 Core 目录提供，不能通过配置替换")
        else:
            for key in TOOL_FIELDS:
                item.setdefault(key, copy.deepcopy(tools[item["id"]][key]))
            if any(not item[key].strip() for key in TOOL_FIELDS if key != "inputSchema"):
                raise ApiError(422, "INVALID_OFFICIAL_CONTENT", "工具名称和说明不能为空")
            validate_schema(item["inputSchema"], tools[item["id"]]["inputSchema"], item["id"])
    if kind == "tools" and set(ids) != set(tools):
        raise ApiError(422, "INCOMPLETE_TOOL_POLICY", "请保留完整工具清单，通过配置管理工具")
    reject_credentials(result)
    return result


def defaults(kind):
    if kind == "skills":
        return validate_items(kind, copy.deepcopy(catalog()["skills"]))
    return [
        {
            "id": t["id"],
            "enabled": t["enabled"],
            "min_implementation_version": t["version"],
            **{key: copy.deepcopy(t[key]) for key in TOOL_FIELDS},
        }
        for t in catalog()["tools"]
    ]


def audit(db, staff, action, **detail):
    db.add(AccountAudit(actor_id=staff.admin_id, action=action, detail=detail, created_at=time.time()))


def descriptor(row):
    return {
        "id": row.id,
        "kind": row.kind,
        "version": row.version,
        "min_core_version": row.min_core_version,
        "notes": row.notes,
        "sha256": row.sha256,
        "enabled": row.enabled,
        "created_at": row.created_at,
        "download_path": f"/api/core/v1/official-content/{row.kind}/{row.id}",
        "schema_version": json.loads(row.payload)["schema_version"],
    }


def available_content(db, core_version, content_protocol=1):
    result = {}
    for kind, key in (("skills", "system_skills"), ("tools", "tools")):
        rows = db.scalars(
            select(OfficialContentRelease)
            .where(OfficialContentRelease.kind == kind, OfficialContentRelease.enabled.is_(True))
            .order_by(OfficialContentRelease.version.desc())
        )
        row = next(
            (
                r
                for r in rows
                if version_tuple(r.min_core_version) <= core_version
                and json.loads(r.payload)["schema_version"] <= content_protocol
            ),
            None,
        )
        result[key] = descriptor(row) if row else None
    return result


class DraftInput(Strict):
    revision: int = Field(strict=True, ge=0)
    items: list[dict] = Field(min_length=1, max_length=200)


class PublishInput(Strict):
    revision: int = Field(strict=True, ge=1)
    version: int = Field(strict=True, ge=1, le=2147483647)
    min_core_version: str = Field(max_length=40)
    notes: str = Field(min_length=1, max_length=8000)


@admin.get("/{kind}")
def read_draft(kind: Kind, db=Depends(get_db)):
    row = db.get(OfficialContentDraft, kind)
    items = row.items if row else defaults(kind)
    warnings = []
    if row and kind == "tools":
        previous = {item["id"]: item for item in row.items}
        # A new compiled catalog may add/remove tools or bump implementations. Preserve compatible configuration;
        # additions need an explicit administrator choice before publication.
        items = []
        for item in defaults(kind):
            old = previous.get(item["id"], {})
            merged = {**item, **{k: old[k] for k in TOOL_FIELDS if k in old}, "enabled": old.get("enabled", False)}
            try:
                validate_schema(merged["inputSchema"], item["inputSchema"], item["id"])
            except ApiError:
                merged["inputSchema"] = item["inputSchema"]
                warnings.append(
                    f"{item['id']} 的旧参数与新 Core 目录不兼容，草稿显示内置参数；原发布内容仍保留在历史中。"
                )
            items.append(merged)
    latest = db.scalar(
        select(OfficialContentRelease)
        .where(OfficialContentRelease.kind == kind)
        .order_by(OfficialContentRelease.version.desc())
        .limit(1)
    )
    return {
        "kind": kind,
        "revision": row.revision if row else 0,
        "items": items,
        "next_version": latest.version + 1 if latest else 1,
        "catalog_core_version": catalog()["core_version"],
        "tool_catalog": catalog()["tools"],
        "catalog_warnings": warnings,
    }


@admin.put("/{kind}")
def save_draft(kind: Kind, body: DraftInput, staff=Depends(require_admin), db=Depends(get_db)):
    items = validate_items(kind, body.items)
    try:
        if body.revision == 0:
            db.add(OfficialContentDraft(kind=kind, revision=1, items=items))
        elif (
            db.execute(
                update(OfficialContentDraft)
                .where(OfficialContentDraft.kind == kind, OfficialContentDraft.revision == body.revision)
                .values(items=items, revision=body.revision + 1)
            ).rowcount
            != 1
        ):
            raise ApiError(409, "DRAFT_CONFLICT", "草稿已改变，请刷新后重试")
        audit(db, staff, "official_draft", kind=kind, revision=body.revision + 1)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "DRAFT_CONFLICT", "草稿已改变，请刷新后重试")
    return read_draft(kind, db)


@admin.get("/{kind}/releases")
def releases(kind: Kind, db=Depends(get_db)):
    return [
        descriptor(r)
        for r in db.scalars(
            select(OfficialContentRelease)
            .where(OfficialContentRelease.kind == kind)
            .order_by(OfficialContentRelease.version.desc())
            .limit(200)
        )
    ]


@admin.post("/{kind}/releases", status_code=201)
def publish(kind: Kind, body: PublishInput, staff=Depends(require_admin), db=Depends(get_db)):
    if version_tuple(body.min_core_version) < version_tuple(catalog()["core_version"]):
        raise ApiError(422, "INCOMPATIBLE_CORE", "最低 Core 版本不能低于所用目录版本")
    # Serialize draft saves/publications. Concurrent publication cannot silently publish a stale draft.
    if (
        db.execute(
            update(OfficialContentDraft)
            .where(OfficialContentDraft.kind == kind, OfficialContentDraft.revision == body.revision)
            .values(revision=body.revision + 1)
        ).rowcount
        != 1
    ):
        raise ApiError(409, "DRAFT_CONFLICT", "请先保存最新草稿，再发布")
    draft = db.get(OfficialContentDraft, kind)
    previous = db.scalar(
        select(OfficialContentRelease)
        .where(OfficialContentRelease.kind == kind)
        .order_by(OfficialContentRelease.version.desc())
        .limit(1)
    )
    if previous and body.version <= previous.version:
        raise ApiError(409, "RELEASE_VERSION_CONFLICT", "发布版本必须递增，已有版本不能修改")
    items = validate_items(kind, draft.items)
    if kind == "skills":
        old_items = json.loads(previous.payload)["items"] if previous else defaults("skills")
        old_by_id = {item["id"]: item for item in old_items}
        for item in items:
            old = old_by_id.get(item["id"])
            if old:
                changed = {k: v for k, v in item.items() if k != "version"} != {
                    k: v for k, v in old.items() if k != "version"
                }
                item["version"] = max(item["version"], old["version"] + int(changed))
        draft.items = items
    needed = (
        set().union(*(set(s["allowedToolIds"] + s["forbiddenToolIds"]) for s in items)) if kind == "skills" else set()
    )
    required_tools = [
        {"id": t["id"], "min_implementation_version": t["version"]} for t in catalog()["tools"] if t["id"] in needed
    ]
    payload = json.dumps(
        {
            "schema_version": 2 if kind == "tools" else 1,
            "kind": kind,
            "version": body.version,
            "min_core_version": body.min_core_version,
            "required_tools": required_tools,
            "items": items,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(payload.encode()) > 1024 * 1024:
        raise ApiError(422, "CONTENT_TOO_LARGE", "发布内容不能超过 1 MB")
    row = OfficialContentRelease(
        kind=kind,
        version=body.version,
        min_core_version=body.min_core_version,
        notes=body.notes,
        payload=payload,
        sha256=digest(payload),
        created_at=time.time(),
    )
    try:
        db.add(row)
        db.flush()
        audit(db, staff, "official_publish", kind=kind, id=row.id, version=row.version, sha256=row.sha256)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "RELEASE_VERSION_CONFLICT", "发布版本已存在，请刷新")
    return descriptor(row)


def find_release(db, kind, release_id):
    row = db.get(OfficialContentRelease, release_id)
    if not row or row.kind != kind:
        raise ApiError(404, "RELEASE_NOT_FOUND")
    return row


@admin.get("/{kind}/releases/{release_id}")
def read_release(kind: Kind, release_id: str, db=Depends(get_db)):
    row = find_release(db, kind, release_id)
    return {**descriptor(row), "content": row.payload}


@admin.delete("/{kind}/releases/{release_id}")
def withdraw(kind: Kind, release_id: str, staff=Depends(require_admin), db=Depends(get_db)):
    row = find_release(db, kind, release_id)
    row.enabled = False
    audit(db, staff, "official_withdraw", kind=kind, id=row.id, version=row.version)
    db.commit()
    return {"ok": True}


@public.get("/{kind}/{release_id}")
def download(kind: Kind, release_id: str, db=Depends(get_db)):
    row = find_release(db, kind, release_id)
    if not row.enabled:
        raise ApiError(410, "RELEASE_WITHDRAWN", "此发布已撤回，请重新检查更新")
    return {**descriptor(row), "content": row.payload}
