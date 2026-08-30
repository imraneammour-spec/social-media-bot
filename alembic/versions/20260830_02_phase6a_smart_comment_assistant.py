"""Add account-scoped Smart Comment Assistant settings.

Revision ID: 20260830_02
Revises: 20260830_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260830_02"
down_revision = "20260830_01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.add_column(
            sa.Column("smart_reply_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column(
                "smart_reply_tone", sa.String(length=32), nullable=False, server_default="friendly"
            )
        )
        batch_op.add_column(
            sa.Column("smart_reply_max_per_hour", sa.Integer(), nullable=False, server_default="20")
        )


def downgrade():
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.drop_column("smart_reply_max_per_hour")
        batch_op.drop_column("smart_reply_tone")
        batch_op.drop_column("smart_reply_enabled")
