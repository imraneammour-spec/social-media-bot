"""Add nullable Prompt opening and Link destination configuration.

Revision ID: 20260906_01
Revises: 20260904_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260906_01"
down_revision = "20260904_02"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.add_column(sa.Column("prompt_opening_message_text", sa.Text()))
        batch_op.add_column(sa.Column("destination_url", sa.Text()))


def downgrade():
    bind = op.get_bind()
    has_v11_configuration = bind.execute(sa.text(
        "SELECT 1 FROM instagram_automation_configs "
        "WHERE prompt_opening_message_text IS NOT NULL "
        "OR destination_url IS NOT NULL LIMIT 1"
    )).first()
    if has_v11_configuration:
        raise RuntimeError("Cannot downgrade while V1.1 automation data exists.")
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.drop_column("destination_url")
        batch_op.drop_column("prompt_opening_message_text")
