"""Bounded task correlation; identity is derived from server authentication."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind in [
        ("user_id", sa.String(64)),
        ("task_id", sa.String(128)),
        ("round_id", sa.String(128)),
        ("step_id", sa.String(128)),
        ("operation", sa.String(40)),
        ("phase_index", sa.Integer()),
        ("client_request_id", sa.String(128)),
    ]:
        op.add_column("model_calls", sa.Column(name, kind, nullable=True))
    for name in ("user_id", "task_id", "client_request_id"):
        op.create_index(f"ix_model_calls_{name}", "model_calls", [name])
    # The reservation FK establishes ownership; never infer a user from a free-text key owner.
    op.execute("""UPDATE model_calls SET user_id = (
        SELECT user_id FROM model_reservations WHERE model_reservations.id = model_calls.id
    ) WHERE EXISTS (SELECT 1 FROM model_reservations WHERE model_reservations.id = model_calls.id)""")


def downgrade():
    raise RuntimeError("Restore a verified backup; call history is never dropped automatically.")
