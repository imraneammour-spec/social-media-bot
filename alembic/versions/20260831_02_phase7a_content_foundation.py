"""Add the minimal durable account-scoped content drafts model.

Revision ID: 20260831_02
Revises: 20260831_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_02"
down_revision = "20260831_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_content_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("instagram_account_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column("content_type", sa.String(length=32), server_default="image", nullable=False),
        sa.Column("caption", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("media_url", sa.Text(), nullable=False),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=True),
        sa.ForeignKeyConstraint(["instagram_account_id"], ["instagram_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_content_items_account_status", "instagram_content_items", ["instagram_account_id", "status"], unique=False)


def downgrade():
    op.drop_index("ix_content_items_account_status", table_name="instagram_content_items")
    op.drop_table("instagram_content_items")
