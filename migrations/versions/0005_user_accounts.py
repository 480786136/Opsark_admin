"""Introduce end-user identities and Token ledger without rewriting legacy model keys."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    # Freeze the schema here: future ORM edits must not rewrite this migration.
    op.create_table(
        "user_accounts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("access_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("refresh_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("access_expires", sa.Float(), nullable=False),
        sa.Column("refresh_expires", sa.Float(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_table(
        "registration_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("initial_tokens", sa.BigInteger(), nullable=False),
        sa.Column("allowed_models", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(64), sa.ForeignKey("admins.id")),
    )
    op.create_table(
        "credit_accounts",
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_accounts.id"), primary_key=True),
        sa.Column("available", sa.BigInteger(), nullable=False),
        sa.Column("reserved", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("available >= 0"),
        sa.CheckConstraint("reserved >= 0"),
    )
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("operation_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("delta", sa.BigInteger(), nullable=False),
        sa.Column("reserved_delta", sa.BigInteger(), nullable=False),
        sa.Column("available_after", sa.BigInteger(), nullable=False),
        sa.Column("reserved_after", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("user_id", "operation_key"),
    )
    op.create_index("ix_credit_ledger_user_id", "credit_ledger", ["user_id"])
    op.create_table(
        "model_reservations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("actual", sa.BigInteger()),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key"),
    )
    op.create_index("ix_model_reservations_user_id", "model_reservations", ["user_id"])
    op.create_table(
        "account_audit",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )


def downgrade():
    raise RuntimeError("Restore a verified backup; user identities and credit ledgers are never dropped automatically.")
