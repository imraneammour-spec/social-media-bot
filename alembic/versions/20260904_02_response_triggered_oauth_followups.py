"""Persist response-triggered direct-message follow-up eligibility.

Revision ID: 20260904_02
Revises: 20260904_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260904_02"
down_revision = "20260904_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "instagram_oauth_direct_message_interactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instagram_account_id", sa.Uuid(), nullable=False),
        sa.Column("source_comment_id", sa.String(255), nullable=False),
        sa.Column("recipient_id", sa.String(255), nullable=False),
        sa.Column("initial_private_reply_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recipient_reply_message_id", sa.String(255)),
        sa.Column("recipient_reply_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["instagram_account_id"], ["instagram_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instagram_account_id", "source_comment_id",
            name="uq_oauth_direct_interaction_account_source_comment",
        ),
    )
    op.create_index(
        "ix_oauth_direct_interaction_account_recipient_reply",
        "instagram_oauth_direct_message_interactions",
        ["instagram_account_id", "recipient_id", "recipient_reply_at"],
    )
    with op.batch_alter_table("instagram_oauth_followup_jobs") as batch_op:
        batch_op.add_column(sa.Column("provider_window_expires_at", sa.DateTime(timezone=True)))


def downgrade():
    bind = op.get_bind()
    has_response_triggered_state = bind.execute(sa.text(
        "SELECT 1 FROM instagram_oauth_direct_message_interactions LIMIT 1"
    )).first() or bind.execute(sa.text(
        "SELECT 1 FROM instagram_oauth_followup_jobs "
        "WHERE provider_window_expires_at IS NOT NULL LIMIT 1"
    )).first()
    if has_response_triggered_state:
        raise RuntimeError("Cannot downgrade while response-triggered follow-up data exists.")
    with op.batch_alter_table("instagram_oauth_followup_jobs") as batch_op:
        batch_op.drop_column("provider_window_expires_at")
    op.drop_index(
        "ix_oauth_direct_interaction_account_recipient_reply",
        table_name="instagram_oauth_direct_message_interactions",
    )
    op.drop_table("instagram_oauth_direct_message_interactions")
