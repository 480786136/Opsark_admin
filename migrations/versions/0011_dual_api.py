"""Explicit route protocols, versioned capabilities and complete model usage."""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    additions = {
        "model_routes": [
            sa.Column("api_protocol", sa.String(32), nullable=False, server_default="chat_completions"),
            sa.Column("structured_output", sa.String(32), nullable=False, server_default="unknown"),
            sa.Column("capabilities_v2_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("budget_semantics", sa.String(32), nullable=False, server_default="unknown"),
        ],
        "model_calls": [
            sa.Column("cached_tokens", sa.Integer(), nullable=True),
            sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
            sa.Column("usage_raw", sa.JSON(), nullable=True),
            sa.Column("api_protocol", sa.String(32), nullable=False, server_default="chat_completions"),
            sa.Column("raw_status", sa.String(40), nullable=True),
        ],
    }
    for table, columns in additions.items():
        existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
                if table == "model_routes" and column.name == "capabilities_v2_enabled":
                    # Existing custom integrations retain the old catalog contract.
                    # ORM-created routes opt into V2; no unknown capability is promoted.
                    op.execute(sa.text("UPDATE model_routes SET capabilities_v2_enabled = true WHERE preset_id != 'custom'"))


def downgrade():
    raise RuntimeError("Restore a verified backup; protocol and usage history cannot be discarded automatically.")
