"""Anonymous feedback and client contact defaults."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("support_tickets") as batch:
        batch.alter_column("user_id", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("guest_submission_key", sa.String(64)))
        batch.add_column(sa.Column("request_hash", sa.String(64)))
        batch.create_unique_constraint("uq_support_guest_submission", ["guest_submission_key"])
    op.add_column("client_policy", sa.Column("support_wechat", sa.String(100), nullable=False, server_default="zgkjkj"))
    op.add_column("client_policy", sa.Column("developer_name", sa.String(100), nullable=False, server_default="智明"))
    op.execute("UPDATE client_policy SET support_email = 'zgkj@zgspace.cn' WHERE support_email = ''")


def downgrade():
    raise RuntimeError("Restore a verified backup; feedback content is never dropped automatically.")
