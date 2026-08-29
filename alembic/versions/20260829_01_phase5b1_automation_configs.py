"""Add tenant-owned OAuth automation configuration with safe backfill.

Revision ID: 20260829_01
Revises: 20260827_03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session

from database.tenant_automation import backfill_oauth_automation_configs


revision = "20260829_01"
down_revision = "20260827_03"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_automation_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instagram_account_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trigger_keywords", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("public_reply_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("public_reply_text", sa.Text(), nullable=False, server_default=""),
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
        sa.UniqueConstraint("instagram_account_id", name="uq_automation_config_account"),
    )
    session = Session(bind=op.get_bind(), join_transaction_mode="rollback_only")
    try:
        backfill_oauth_automation_configs(session)
        session.flush()
    finally:
        session.close()


def downgrade():
    op.drop_table("instagram_automation_configs")
