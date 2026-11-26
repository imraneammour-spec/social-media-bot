"""Big batch: legacy JSON state -> Postgres, BYOK keys, email verification.

Creates the four legacy-bot tables (durable replacements for the JSON
state files that Heroku's ephemeral disk wiped on every deploy), the
per-user encrypted Gemini key table (BYOK), and the email-verified
timestamp on users (email verification before Instagram connect).
"""
from alembic import op
import sqlalchemy as sa

revision = "20261126_02"
down_revision = "20261126_01"
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    ]


def upgrade():
    op.create_table(
        "legacy_processed_comments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "instagram_account_id",
            sa.Uuid(),
            sa.ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("comment_id", sa.String(255), nullable=False),
        sa.Column(
            "state",
            sa.String(32),
            nullable=False,
            server_default="processing",
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instagram_account_id",
            "comment_id",
            name="uq_legacy_processed_account_comment",
        ),
        sa.Index(
            "ix_legacy_processed_account_created",
            "instagram_account_id",
            "created_at",
        ),
    )
    op.create_table(
        "legacy_prompt_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "instagram_account_id",
            sa.Uuid(),
            sa.ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(512), nullable=False),
        sa.Column("media_id", sa.String(255), nullable=False),
        sa.Column("comment_id", sa.String(255), nullable=False),
        sa.Column("commenter_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("page_url", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.Index(
            "ix_legacy_prompt_requests_account_key_created",
            "instagram_account_id",
            "request_key",
            "created_at",
        ),
    )
    op.create_table(
        "legacy_pending_access",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "instagram_account_id",
            sa.Uuid(),
            sa.ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recipient_id", sa.String(255), nullable=False),
        sa.Column("page_url", sa.Text(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instagram_account_id",
            "recipient_id",
            name="uq_legacy_pending_account_recipient",
        ),
    )
    op.create_table(
        "legacy_prompt_posts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "instagram_account_id",
            sa.Uuid(),
            sa.ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("media_id", sa.String(255), nullable=False),
        sa.Column("permalink", sa.Text(), nullable=True),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("prompts", sa.JSON(), nullable=False),
        sa.Column("prompt_count", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instagram_account_id",
            "media_id",
            name="uq_legacy_prompt_post_account_media",
        ),
    )
    op.create_table(
        "user_gemini_keys",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_ciphertext", sa.Text(), nullable=True),
        sa.Column("encryption_key_id", sa.String(255), nullable=True),
        sa.Column("key_hint", sa.String(8), nullable=True),
        sa.Column(
            "key_status",
            sa.String(32),
            nullable=False,
            server_default="active",
        ),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("users", "email_verified_at")
    op.drop_table("user_gemini_keys")
    op.drop_table("legacy_prompt_posts")
    op.drop_table("legacy_pending_access")
    op.drop_table("legacy_prompt_requests")
    op.drop_table("legacy_processed_comments")
