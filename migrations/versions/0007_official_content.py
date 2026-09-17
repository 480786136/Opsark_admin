"""Official Skill and tool-policy drafts and immutable releases."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "official_content_drafts",
        sa.Column("kind", sa.String(20), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
    )
    op.create_table(
        "official_content_releases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("min_core_version", sa.String(40), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("kind", "version"),
    )
    op.create_index("ix_official_content_releases_kind", "official_content_releases", ["kind"])


def downgrade():
    raise RuntimeError("Restore a verified backup; release history is not dropped automatically.")
