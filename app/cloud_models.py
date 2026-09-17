"""Private user resources and public client release metadata, never executable plugins."""

import uuid

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class UserSkill(Base):
    __tablename__ = "user_skills"
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    encrypted_payload: Mapped[str | None] = mapped_column(Text)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[float] = mapped_column(Float)


class CloudMutation(Base):
    __tablename__ = "cloud_mutations"
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    guest_submission_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="open")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    encrypted_payload: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


class ClientPolicy(Base):
    __tablename__ = "client_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    support_email: Mapped[str] = mapped_column(String(254), default="zgkj@zgspace.cn")
    support_wechat: Mapped[str] = mapped_column(String(100), default="zgkjkj")
    developer_name: Mapped[str] = mapped_column(String(100), default="智明")
    support_url: Mapped[str] = mapped_column(String(1000), default="")
    min_cloud_version: Mapped[str] = mapped_column(String(40), default="0.0.0")
    revision: Mapped[int] = mapped_column(Integer, default=0)


class ClientRelease(Base):
    __tablename__ = "client_releases"
    __table_args__ = (UniqueConstraint("platform", "arch", "version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    platform: Mapped[str] = mapped_column(String(20))
    arch: Mapped[str] = mapped_column(String(20))
    version: Mapped[str] = mapped_column(String(40))
    notes: Mapped[str] = mapped_column(Text)
    download_url: Mapped[str] = mapped_column(String(2000))
    sha256: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float)


class GithubIdentity(Base):
    __tablename__ = "github_identities"
    github_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), unique=True)


class GithubFlow(Base):
    __tablename__ = "github_flows"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    native_challenge: Mapped[str] = mapped_column(String(64))
    encrypted_verifier: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    user_id: Mapped[str | None] = mapped_column(ForeignKey("user_accounts.id"))
    mode: Mapped[str] = mapped_column(String(10))
    expires_at: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(100))


class OfficialContentDraft(Base):
    __tablename__ = "official_content_drafts"
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    items: Mapped[list] = mapped_column(JSON)


class OfficialContentRelease(Base):
    __tablename__ = "official_content_releases"
    __table_args__ = (UniqueConstraint("kind", "version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    version: Mapped[int] = mapped_column(Integer)
    min_core_version: Mapped[str] = mapped_column(String(40))
    notes: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float)
