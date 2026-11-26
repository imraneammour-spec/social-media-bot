"""Add durable account-scoped OAuth delayed follow-up jobs.

Revision ID: 20260827_02
Revises: 20260827_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260827_02"
down_revision = "20260827_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_oauth_followup_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instagram_account_id", sa.Uuid(), nullable=False),
        sa.Column("prompt_request_id", sa.Uuid()),
        sa.Column("source_message_id", sa.String(255), nullable=False),
        sa.Column("recipient_id", sa.String(255), nullable=False),
        sa.Column("page_url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["instagram_account_id"], ["instagram_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["prompt_request_id"], ["instagram_prompt_requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instagram_account_id", "source_message_id", name="uq_oauth_followup_account_source_message"),
    )
    op.create_index(
        "uq_oauth_followup_active_account_recipient",
        "instagram_oauth_followup_jobs",
        ["instagram_account_id", "recipient_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'processing')"),
        sqlite_where=sa.text("state IN ('pending', 'processing')"),
    )
    op.create_index("ix_oauth_followup_state_run_at", "instagram_oauth_followup_jobs", ["state", "run_at"])
    op.create_index("ix_oauth_followup_account_recipient", "instagram_oauth_followup_jobs", ["instagram_account_id", "recipient_id"])


def downgrade():
    op.drop_table("instagram_oauth_followup_jobs")
