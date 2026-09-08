"""Add model gateway metadata without rewriting existing platform data."""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    names = set(sa.inspect(op.get_bind()).get_table_names())
    definitions = {
        "model_providers": [
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("base_url", sa.String(1000), nullable=False),
            sa.Column("encrypted_key", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        ],
        "model_routes": [
            sa.Column("alias", sa.String(128), unique=True, nullable=False),
            sa.Column("provider_id", sa.String(64), sa.ForeignKey("model_providers.id"), nullable=False),
            sa.Column("upstream_model", sa.String(200), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
        ],
        "model_keys": [
            sa.Column("owner", sa.String(128), nullable=False),
            sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
            sa.Column("prefix", sa.String(24), nullable=False),
            sa.Column("allowed_models", sa.JSON(), nullable=False),
            sa.Column("expires", sa.Float(), nullable=False),
            sa.Column("revoked", sa.Boolean(), nullable=False),
            sa.Column("rpm", sa.Integer(), nullable=False),
        ],
        "model_calls": [
            sa.Column("key_id", sa.String(64), nullable=False),
            sa.Column("owner", sa.String(128), nullable=False),
            sa.Column("provider_id", sa.String(64), nullable=False),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("started_at", sa.Float(), nullable=False),
            sa.Column("duration_ms", sa.Integer()),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("http_status", sa.Integer()),
            sa.Column("input_tokens", sa.Integer()),
            sa.Column("output_tokens", sa.Integer()),
            sa.Column("error_code", sa.String(64)),
        ],
    }
    for table, columns in definitions.items():
        if table not in names:
            op.create_table(table, sa.Column("id", sa.String(64), primary_key=True), *columns)
            if table == "model_calls":
                op.create_index("ix_model_calls_key_id", table, ["key_id"])
                op.create_index("ix_model_calls_started_at", table, ["started_at"])


def downgrade():
    raise RuntimeError("Restore a verified backup; gateway data is not deleted automatically.")
