"""Add durable OAuth webhook ingress work items.

Revision ID: 20260830_01
Revises: 20260829_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260830_01"
down_revision = "20260829_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_oauth_webhook_work_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instagram_account_id", sa.Uuid(), nullable=False),
        sa.Column("instagram_user_id", sa.String(length=64), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("public_base_url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["instagram_account_id"], ["instagram_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instagram_account_id", "provider_event_id", "event_type",
            name="uq_oauth_webhook_work_account_event_type",
        ),
    )
    op.create_index(
        "ix_oauth_webhook_work_state_lease",
        "instagram_oauth_webhook_work_items",
        ["state", "lease_expires_at"],
    )
    op.create_index(
        "ix_oauth_webhook_work_account_state",
        "instagram_oauth_webhook_work_items",
        ["instagram_account_id", "state"],
    )


def downgrade():
    op.drop_index("ix_oauth_webhook_work_account_state", table_name="instagram_oauth_webhook_work_items")
    op.drop_index("ix_oauth_webhook_work_state_lease", table_name="instagram_oauth_webhook_work_items")
    op.drop_table("instagram_oauth_webhook_work_items")
