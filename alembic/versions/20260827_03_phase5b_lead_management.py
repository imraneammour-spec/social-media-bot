"""Add tenant-scoped lead metadata to OAuth prompt requests.

Revision ID: 20260827_03
Revises: 20260827_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260827_03"
down_revision = "20260827_02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "instagram_prompt_requests",
        sa.Column("lead_status", sa.String(length=16), nullable=False, server_default="new"),
    )
    op.add_column("instagram_prompt_requests", sa.Column("internal_note", sa.Text(), nullable=True))
    op.add_column(
        "instagram_prompt_requests",
        sa.Column("lead_archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_prompt_requests_account_lead_state_created",
        "instagram_prompt_requests",
        ["instagram_account_id", "lead_archived_at", "lead_status", "created_at"],
    )


def downgrade():
    op.drop_index("ix_prompt_requests_account_lead_state_created", table_name="instagram_prompt_requests")
    op.drop_column("instagram_prompt_requests", "lead_archived_at")
    op.drop_column("instagram_prompt_requests", "internal_note")
    op.drop_column("instagram_prompt_requests", "lead_status")
