"""End-user identities and transactional Token balances; separate from staff sessions."""

import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class UserAccount(Base):
    __tablename__ = "user_accounts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float)


class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    access_expires: Mapped[float] = mapped_column(Float)
    refresh_expires: Mapped[float] = mapped_column(Float)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class RegistrationPolicy(Base):
    __tablename__ = "registration_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    initial_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    allowed_models: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("admins.id"))


class CreditAccount(Base):
    __tablename__ = "credit_accounts"
    __table_args__ = (CheckConstraint("available >= 0"), CheckConstraint("reserved >= 0"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), primary_key=True)
    available: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved: Mapped[int] = mapped_column(BigInteger, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class CreditLedger(Base):
    __tablename__ = "credit_ledger"
    __table_args__ = (UniqueConstraint("user_id", "operation_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    operation_key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))
    delta: Mapped[int] = mapped_column(BigInteger)
    reserved_delta: Mapped[int] = mapped_column(BigInteger, default=0)
    available_after: Mapped[int] = mapped_column(BigInteger)
    reserved_after: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(500))
    actor_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float)


class ModelReservation(Base):
    __tablename__ = "model_reservations"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    amount: Mapped[int] = mapped_column(BigInteger)
    actual: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(32), default="reserved")
    created_at: Mapped[float] = mapped_column(Float)


class AccountAudit(Base):
    __tablename__ = "account_audit"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    actor_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)
