"""Add durable scheduled-publishing lifecycle metadata for Phase 7C.

Revision ID: 20260831_04
Revises: 20260831_03
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_04"
down_revision = "20260831_03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("instagram_content_items", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("instagram_content_items", sa.Column("provider_media_id", sa.String(length=255), nullable=True))
    op.add_column("instagram_content_items", sa.Column("publish_error_code", sa.String(length=128), nullable=True))
    op.add_column(
        "instagram_content_items",
        sa.Column(
            "provider_request_may_have_been_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_content_items_status_scheduled_for",
        "instagram_content_items",
        ["status", "scheduled_for"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_content_items_status_scheduled_for", table_name="instagram_content_items")
    op.drop_column("instagram_content_items", "provider_request_may_have_been_sent")
    op.drop_column("instagram_content_items", "publish_error_code")
    op.drop_column("instagram_content_items", "provider_media_id")
    op.drop_column("instagram_content_items", "published_at")
