"""Public, read-only update guidance; no arbitrary client code delivery or forced restart."""

# ruff: noqa: B008
import re
import time
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query
from pydantic import Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .accounts import Strict
from .cloud_common import secure_url, version_tuple
from .cloud_models import ClientPolicy, ClientRelease
from .config import settings
from .db import get_db
from .official_content import available_content
from .security import ApiError, require_admin
from .user_models import AccountAudit

public = APIRouter(prefix="/api/core/v1")
admin = APIRouter(prefix="/api/admin/v1", dependencies=[Depends(require_admin)])


def policy(db):
    return db.get(ClientPolicy, 1) or SimpleNamespace(
        support_email="zgkj@zgspace.cn",
        support_wechat="zgkjkj",
        developer_name="智明",
        support_url="",
        min_cloud_version="0.0.0",
        revision=0,
    )


class ClientPolicyInput(Strict):
    support_email: str = Field(default="zgkj@zgspace.cn", max_length=254)
    support_wechat: str = Field(default="zgkjkj", max_length=100)
    developer_name: str = Field(default="智明", max_length=100)
    support_url: str = Field(default="", max_length=1000)
    min_cloud_version: str = "0.0.0"
    revision: int = Field(strict=True, ge=0)

    @field_validator("support_email")
    @classmethod
    def email(cls, value):
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("invalid email")
        return value


@admin.get("/client-policy")
def get_policy(db=Depends(get_db)):
    p = policy(db)
    return {
        "support_email": p.support_email,
        "support_wechat": p.support_wechat,
        "developer_name": p.developer_name,
        "support_url": p.support_url,
        "min_cloud_version": p.min_cloud_version,
        "revision": p.revision,
    }


@admin.put("/client-policy")
def save_policy(body: ClientPolicyInput, staff=Depends(require_admin), db=Depends(get_db)):
    version_tuple(body.min_cloud_version)
    if body.support_url:
        secure_url(body.support_url)
    values = body.model_dump()
    values["revision"] += 1
    try:
        if db.get(ClientPolicy, 1) is None and body.revision == 0:
            db.add(ClientPolicy(id=1, **values))
        elif (
            db.execute(
                update(ClientPolicy)
                .where(ClientPolicy.id == 1, ClientPolicy.revision == body.revision)
                .values(**values)
            ).rowcount
            != 1
        ):
            raise ApiError(409, "POLICY_CONFLICT", "策略已改变，请刷新")
        db.add(AccountAudit(actor_id=staff.admin_id, action="client_policy", detail=values, created_at=time.time()))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "POLICY_CONFLICT")
    return get_policy(db)


class ReleaseInput(Strict):
    platform: Literal["macos", "windows", "linux"]
    arch: Literal["aarch64", "x86_64"]
    version: str = Field(max_length=40)
    notes: str = Field(min_length=1, max_length=8000)
    download_url: str = Field(max_length=2000)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


def public_release(r):
    return {
        "id": r.id,
        "platform": r.platform,
        "arch": r.arch,
        "version": r.version,
        "notes": r.notes,
        "download_url": r.download_url,
        "sha256": r.sha256,
        "enabled": r.enabled,
        "created_at": r.created_at,
    }


@admin.get("/client-releases")
def releases(db=Depends(get_db)):
    return [
        public_release(r)
        for r in db.scalars(select(ClientRelease).order_by(ClientRelease.created_at.desc()).limit(200))
    ]


@admin.post("/client-releases", status_code=201)
def publish(body: ReleaseInput, staff=Depends(require_admin), db=Depends(get_db)):
    version_tuple(body.version)
    secure_url(body.download_url)
    hosts = {h.strip().lower() for h in settings().release_allowed_hosts.split(",") if h.strip()}
    if urlsplit(body.download_url).hostname not in hosts:
        raise ApiError(422, "RELEASE_HOST_NOT_ALLOWED", "请先在服务端 RELEASE_ALLOWED_HOSTS 配置下载域名白名单")
    row = ClientRelease(**body.model_dump(), created_at=time.time(), enabled=True)
    try:
        db.add(row)
        db.flush()
        db.add(
            AccountAudit(
                actor_id=staff.admin_id,
                action="publish_release",
                detail={"id": row.id, "version": row.version},
                created_at=time.time(),
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "RELEASE_EXISTS", "该平台版本已发布，不可原地替换，请发布新版本")
    return public_release(row)


@admin.delete("/client-releases/{release_id}")
def withdraw(release_id: str, staff=Depends(require_admin), db=Depends(get_db)):
    row = db.get(ClientRelease, release_id)
    if not row:
        raise ApiError(404, "RELEASE_NOT_FOUND")
    row.enabled = False
    db.add(
        AccountAudit(actor_id=staff.admin_id, action="withdraw_release", detail={"id": row.id}, created_at=time.time())
    )
    db.commit()
    return {"ok": True}


@public.get("/client-info")
def client_info(
    platform: Literal["macos", "windows", "linux"],
    arch: Literal["aarch64", "x86_64"],
    version: str,
    db=Depends(get_db),
    content_protocol: int = Query(default=1, ge=1, le=2),
):
    current = version_tuple(version)
    p = policy(db)
    rows = list(
        db.scalars(
            select(ClientRelease).where(
                ClientRelease.enabled.is_(True), ClientRelease.platform == platform, ClientRelease.arch == arch
            )
        )
    )
    latest = max(rows, key=lambda r: version_tuple(r.version)) if rows else None
    return {
        "support_email": p.support_email,
        "support_wechat": p.support_wechat,
        "developer_name": p.developer_name,
        "contact_revision": p.revision,
        "support_url": p.support_url,
        "feedback_retention_days": 30,
        "min_cloud_version": p.min_cloud_version,
        "update_required": current < version_tuple(p.min_cloud_version),
        "latest": public_release(latest) if latest and version_tuple(latest.version) > current else None,
        **available_content(db, current, content_protocol),
    }
