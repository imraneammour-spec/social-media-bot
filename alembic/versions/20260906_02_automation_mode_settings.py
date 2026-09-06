"""Retain inactive automation-mode presentation settings.

Revision ID: 20260906_02
Revises: 20260906_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260906_02"
down_revision = "20260906_01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.add_column(sa.Column("inactive_mode_settings", sa.JSON(), nullable=True))


def downgrade():
    bind = op.get_bind()
    has_saved_settings = bind.execute(sa.text(
        "SELECT 1 FROM instagram_automation_configs "
        "WHERE inactive_mode_settings IS NOT NULL LIMIT 1"
    )).first()
    if has_saved_settings:
        raise RuntimeError(
            "Cannot downgrade while inactive automation mode settings exist."
        )
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.drop_column("inactive_mode_settings")
