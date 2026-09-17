"""Initial schema. Future schema changes require a separate explicit migration."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    # Freeze the original two tables. Importing current ORM metadata here made
    # fresh installs create later tables before their migrations ran.
    names = set(sa.inspect(op.get_bind()).get_table_names())
    if "admins" not in names:
        op.create_table(
            "admins",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("username", sa.String(100), unique=True, nullable=False),
            sa.Column("password_hash", sa.Text(), nullable=False),
        )
    if "sessions" not in names:
        op.create_table(
            "sessions",
            sa.Column("token_hash", sa.String(64), primary_key=True),
            sa.Column("admin_id", sa.String(64), sa.ForeignKey("admins.id"), nullable=False),
            sa.Column("csrf", sa.String(100), nullable=False),
            sa.Column("expires", sa.Float(), nullable=False),
        )


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a verified backup instead.")
