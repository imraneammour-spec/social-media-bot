"""Add the credit lots table for one-time AI credit packs.

One row per purchased pack (see BillingCreditLot in database/models.py):
credits are consumed oldest-first only after the plan's monthly AI
allowance is exhausted, and expires_at stays NULL -- credits never
expire while the account is active (see CREDIT_EXPIRY_DAYS in
database/billing.py). provider_order_id is unique so a duplicate order
webhook can never credit twice.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261127_02"
down_revision = "20261127_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "billing_credit_lots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pack_key", sa.String(32), nullable=False),
        sa.Column("credits_total", sa.Integer(), nullable=False),
        sa.Column("credits_remaining", sa.Integer(), nullable=False),
        sa.Column("provider_order_id", sa.String(64), unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_billing_credit_lots_user_remaining",
        "billing_credit_lots",
        ["user_id", "credits_remaining"],
    )
    op.create_index(
        "ix_billing_credit_lots_user_id", "billing_credit_lots", ["user_id"]
    )


def downgrade():
    op.drop_index("ix_billing_credit_lots_user_id", table_name="billing_credit_lots")
    op.drop_index("ix_billing_credit_lots_user_remaining", table_name="billing_credit_lots")
    op.drop_table("billing_credit_lots")
