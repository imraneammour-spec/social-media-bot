"""Add advisory lead intelligence fields.

Revision ID: 20260830_03
Revises: 20260830_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260830_03"
down_revision = "20260830_02"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("instagram_prompt_requests") as batch_op:
        batch_op.add_column(sa.Column("ai_intent", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("ai_priority", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("ai_summary", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ai_suggested_followup", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ai_analyzed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("instagram_prompt_requests") as batch_op:
        batch_op.drop_column("ai_analyzed_at")
        batch_op.drop_column("ai_suggested_followup")
        batch_op.drop_column("ai_summary")
        batch_op.drop_column("ai_priority")
        batch_op.drop_column("ai_intent")
