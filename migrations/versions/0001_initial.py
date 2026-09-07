"""Initial schema. Future schema changes require a separate explicit migration."""

from alembic import op
from app.db import Base
from app import models  # noqa: F401

revision = "0001"
down_revision = None


def upgrade():
    connection = op.get_bind()
    Base.metadata.create_all(connection)


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a verified backup instead.")
