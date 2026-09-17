"""User Skills, support, release guidance and GitHub sign-in."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_routes", sa.Column("display_name", sa.String(100), nullable=False, server_default=""))
    op.create_table(
        "user_skills",
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_accounts.id"), primary_key=True),
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("encrypted_payload", sa.Text()),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "cloud_mutations",
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_accounts.id"), primary_key=True),
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("encrypted_payload", sa.Text()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_support_tickets_user_id", "support_tickets", ["user_id"])
    op.create_index("ix_support_tickets_expires_at", "support_tickets", ["expires_at"])
    op.create_table(
        "client_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("support_email", sa.String(254), nullable=False),
        sa.Column("support_url", sa.String(1000), nullable=False),
        sa.Column("min_cloud_version", sa.String(40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_table(
        "client_releases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("arch", sa.String(20), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("download_url", sa.String(2000), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("platform", "arch", "version"),
    )
    op.create_table(
        "github_identities",
        sa.Column("github_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_accounts.id"), nullable=False, unique=True),
    )
    op.create_table(
        "github_flows",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("native_challenge", sa.String(64), nullable=False),
        sa.Column("encrypted_verifier", sa.Text()),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("user_accounts.id")),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("error", sa.String(100)),
    )


def downgrade():
    raise RuntimeError("Restore a verified backup; private user resources are never dropped automatically.")
