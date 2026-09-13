"""Encrypted, expiring model call input/output snapshots."""

from alembic import op
from app.models import ModelCallDetail

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    ModelCallDetail.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Restore a verified backup; call details are not deleted automatically.")
