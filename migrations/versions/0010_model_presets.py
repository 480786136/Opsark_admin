"""Opt-in model presets; existing routes retain custom passthrough semantics."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("model_routes")}
    if "preset_id" not in columns:
        op.add_column("model_routes", sa.Column("preset_id", sa.String(64), nullable=False, server_default="custom"))


def downgrade():
    raise RuntimeError("Restore a verified backup; route preset configuration is not deleted automatically.")
