"""Add durable UTC scheduling metadata for Phase 7B content items.

Revision ID: 20260831_03
Revises: 20260831_02
"""

from alembic import op
import sqlalchemy as sa

revision = "20260831_03"
down_revision = "20260831_02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("instagram_content_items", sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_instagram_content_items_scheduled_for", "instagram_content_items", ["scheduled_for"], unique=False)


def downgrade():
    op.drop_index("ix_instagram_content_items_scheduled_for", table_name="instagram_content_items")
    op.drop_column("instagram_content_items", "scheduled_for")
