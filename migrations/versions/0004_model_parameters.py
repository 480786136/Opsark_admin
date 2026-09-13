"""Add validated provider defaults and route-enforced model parameters."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    provider_columns = {column["name"] for column in inspector.get_columns("model_providers")}
    route_columns = {column["name"] for column in inspector.get_columns("model_routes")}
    if "parameter_defaults" not in provider_columns:
        op.add_column(
            "model_providers",
            sa.Column("parameter_defaults", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )
    if "parameter_overrides" not in route_columns:
        op.add_column(
            "model_routes",
            sa.Column("parameter_overrides", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )


def downgrade():
    raise RuntimeError("Restore a verified backup; model parameter configuration is not deleted automatically.")
