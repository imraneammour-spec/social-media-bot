"""Create additive Phase 2 database-foundation tables.

Revision ID: 20260825_01
Revises:
Create Date: 2026-08-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260825_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)

    op.create_table(
        "instagram_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instagram_user_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("connection_status", sa.String(length=32), server_default=sa.text("'environment_managed'"), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instagram_user_id"),
    )
    op.create_index(op.f("ix_instagram_accounts_instagram_user_id"), "instagram_accounts", ["instagram_user_id"], unique=False)
    op.create_index(op.f("ix_instagram_accounts_user_id"), "instagram_accounts", ["user_id"], unique=False)
    op.create_index(op.f("ix_instagram_accounts_username"), "instagram_accounts", ["username"], unique=False)
    op.create_index("ix_instagram_accounts_user_connection", "instagram_accounts", ["user_id", "connection_status"], unique=False)

    op.create_table(
        "instagram_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instagram_account_id", sa.Uuid(), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=True),
        sa.Column("encryption_key_id", sa.String(length=255), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["instagram_account_id"], ["instagram_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instagram_account_id"),
    )

    op.create_table(
        "migration_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("migration_name", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source_record_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("imported_record_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("skipped_record_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("migration_name", "source", name="uq_migration_runs_name_source"),
    )


def downgrade() -> None:
    op.drop_table("migration_runs")
    op.drop_table("instagram_credentials")
    op.drop_index("ix_instagram_accounts_user_connection", table_name="instagram_accounts")
    op.drop_index(op.f("ix_instagram_accounts_username"), table_name="instagram_accounts")
    op.drop_index(op.f("ix_instagram_accounts_user_id"), table_name="instagram_accounts")
    op.drop_index(op.f("ix_instagram_accounts_instagram_user_id"), table_name="instagram_accounts")
    op.drop_table("instagram_accounts")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
