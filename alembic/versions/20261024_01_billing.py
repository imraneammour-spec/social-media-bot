"""Add billing without changing users, content, credentials, or onboarding choices."""
from alembic import op
import sqlalchemy as sa

revision = "20261024_01"
down_revision = "20260907_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("billing_subscriptions",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("provider_id", sa.String(64), unique=True),
        sa.Column("plan", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("interval", sa.String(16)),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("renews_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("billing_checkouts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("variant_id", sa.String(64), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("consumed_by", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_billing_checkouts_user_id", "billing_checkouts", ["user_id"])
    op.create_table("billing_webhooks",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("event_name", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("billing_usage",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("period", sa.String(7), primary_key=True),
        sa.Column("metric", sa.String(32), primary_key=True),
        sa.Column("quantity", sa.Integer(), nullable=False))
    # Existing access is preserved explicitly. Onboarding "Pro" is never proof of payment.
    op.execute(sa.text("INSERT INTO billing_subscriptions (user_id, plan, status, test_mode) SELECT id, 'legacy', 'legacy', false FROM users"))


def downgrade():
    for table in ("billing_checkouts", "billing_usage", "billing_webhooks"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Billing data exists. Restore the verified backup instead of dropping billing records.")
    if op.get_bind().execute(sa.text("SELECT 1 FROM billing_subscriptions WHERE provider_id IS NOT NULL OR plan <> 'legacy' OR status <> 'legacy' LIMIT 1")).first():
        raise RuntimeError("Subscription data exists. Restore the verified backup instead.")
    for table in ("billing_checkouts", "billing_usage", "billing_webhooks", "billing_subscriptions"):
        op.drop_table(table)
