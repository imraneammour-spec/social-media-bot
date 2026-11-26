"""Add Phase 6C follow-up builder fields.

Revision ID: 20260831_01
Revises: 20260830_03
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_01"
down_revision = "20260830_03"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.add_column(sa.Column("followup_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")))
        batch_op.add_column(sa.Column("followup_delay_minutes", sa.Integer(), nullable=False, server_default=sa.text("360")))


def downgrade():
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.drop_column("followup_delay_minutes")
        batch_op.drop_column("followup_enabled")
