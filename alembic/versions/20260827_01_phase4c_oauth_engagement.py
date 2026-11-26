"""Add durable OAuth engagement state without modifying legacy JSON storage.

Revision ID: 20260827_01
Revises: 20260825_02
"""

from alembic import op
import sqlalchemy as sa

revision = "20260827_01"
down_revision = "20260825_02"
branch_labels = None
depends_on = None


def _id_column():
    return sa.Column("id", sa.Uuid(), nullable=False)


def upgrade():
    op.create_table("instagram_engagement_events", _id_column(), sa.Column("instagram_account_id", sa.Uuid(), nullable=False), sa.Column("provider_event_id", sa.String(255), nullable=False), sa.Column("event_type", sa.String(32), nullable=False), sa.Column("state", sa.String(32), nullable=False, server_default="processing"), sa.Column("completed_at", sa.DateTime(timezone=True)), sa.Column("failed_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.ForeignKeyConstraint(["instagram_account_id"], ["instagram_accounts.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("instagram_account_id", "provider_event_id", "event_type", name="uq_engagement_event_account_provider_type"))
    op.create_index("ix_engagement_events_account_state", "instagram_engagement_events", ["instagram_account_id", "state"])
    op.create_table("instagram_prompt_posts", _id_column(), sa.Column("instagram_account_id", sa.Uuid(), nullable=False), sa.Column("media_id", sa.String(255), nullable=False), sa.Column("public_id", sa.String(128), nullable=False), sa.Column("prompts", sa.JSON(), nullable=False), sa.Column("permalink", sa.Text()), sa.Column("cover_url", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.ForeignKeyConstraint(["instagram_account_id"], ["instagram_accounts.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("instagram_account_id", "media_id", name="uq_prompt_post_account_media"), sa.UniqueConstraint("public_id"))
    op.create_index("ix_instagram_prompt_posts_instagram_account_id", "instagram_prompt_posts", ["instagram_account_id"])
    op.create_index("ix_instagram_prompt_posts_public_id", "instagram_prompt_posts", ["public_id"])
    for name, columns, constraints in (("instagram_pending_access", [sa.Column("recipient_id", sa.String(255), nullable=False), sa.Column("page_url", sa.Text(), nullable=False)], [sa.UniqueConstraint("instagram_account_id", "recipient_id", name="uq_pending_access_account_recipient")]), ("instagram_prompt_requests", [sa.Column("media_id", sa.String(255), nullable=False), sa.Column("comment_id", sa.String(255), nullable=False), sa.Column("recipient_id", sa.String(255), nullable=False), sa.Column("username", sa.String(255)), sa.Column("page_url", sa.Text(), nullable=False)], []), ("instagram_smart_reply_activity", [sa.Column("comment_id", sa.String(255), nullable=False), sa.Column("recipient_id", sa.String(255)), sa.Column("username", sa.String(255)), sa.Column("reply_text", sa.Text(), nullable=False)], [])):
        op.create_table(name, _id_column(), sa.Column("instagram_account_id", sa.Uuid(), nullable=False), *columns, sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.ForeignKeyConstraint(["instagram_account_id"], ["instagram_accounts.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"), *constraints)
    op.create_index("ix_instagram_pending_access_instagram_account_id", "instagram_pending_access", ["instagram_account_id"])
    op.create_index("ix_prompt_requests_account_media_recipient", "instagram_prompt_requests", ["instagram_account_id", "media_id", "recipient_id"])
    op.create_index("ix_smart_reply_activity_account_created", "instagram_smart_reply_activity", ["instagram_account_id", "created_at"])


def downgrade():
    for name in ("instagram_smart_reply_activity", "instagram_prompt_requests", "instagram_pending_access", "instagram_prompt_posts", "instagram_engagement_events"):
        op.drop_table(name)
