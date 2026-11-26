"""Separate direct keyword automation from legacy prompt delivery.

Revision ID: 20260904_01
Revises: 20260831_04
"""

from alembic import op
import sqlalchemy as sa


revision = "20260904_01"
down_revision = "20260831_04"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "delivery_mode", sa.String(length=32), nullable=False,
                server_default="prompt_request",
            )
        )
        batch_op.add_column(
            sa.Column("private_message_text", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("followup_message_text", sa.Text(), nullable=False, server_default="")
        )
    with op.batch_alter_table("instagram_oauth_followup_jobs") as batch_op:
        batch_op.alter_column("page_url", existing_type=sa.Text(), nullable=True)
        batch_op.add_column(sa.Column("message_text", sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    has_direct_configuration = bind.execute(sa.text(
        "SELECT 1 FROM instagram_automation_configs "
        "WHERE delivery_mode <> 'prompt_request' "
        "OR private_message_text <> '' OR followup_message_text <> '' LIMIT 1"
    )).first()
    has_general_followup = bind.execute(sa.text(
        "SELECT 1 FROM instagram_oauth_followup_jobs "
        "WHERE page_url IS NULL OR message_text IS NOT NULL LIMIT 1"
    )).first()
    if has_direct_configuration or has_general_followup:
        raise RuntimeError("Cannot downgrade while direct-message automation data exists.")
    with op.batch_alter_table("instagram_oauth_followup_jobs") as batch_op:
        batch_op.drop_column("message_text")
        batch_op.alter_column("page_url", existing_type=sa.Text(), nullable=False)
    with op.batch_alter_table("instagram_automation_configs") as batch_op:
        batch_op.drop_column("followup_message_text")
        batch_op.drop_column("private_message_text")
        batch_op.drop_column("delivery_mode")
