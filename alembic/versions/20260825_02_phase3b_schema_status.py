"""Add Phase 3B.1 Instagram connection and credential lifecycle fields.

Revision ID: 20260825_02
Revises: 20260825_01
Create Date: 2026-08-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260825_02"
down_revision: Union[str, Sequence[str], None] = "20260825_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instagram_accounts",
        sa.Column(
            "provider",
            sa.String(length=32),
            server_default=sa.text("'instagram'"),
            nullable=False,
        ),
    )
    op.add_column(
        "instagram_accounts",
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "instagram_accounts",
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "instagram_accounts",
        sa.Column("last_connection_error_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "instagram_credentials",
        sa.Column("token_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "instagram_credentials",
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "instagram_credentials",
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "instagram_credentials",
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "instagram_credentials",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "instagram_credentials",
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instagram_credentials", "last_error_code")
    op.drop_column("instagram_credentials", "revoked_at")
    op.drop_column("instagram_credentials", "last_refreshed_at")
    op.drop_column("instagram_credentials", "last_validated_at")
    op.drop_column("instagram_credentials", "issued_at")
    op.drop_column("instagram_credentials", "token_status")
    op.drop_column("instagram_accounts", "last_connection_error_code")
    op.drop_column("instagram_accounts", "disconnected_at")
    op.drop_column("instagram_accounts", "connected_at")
    op.drop_column("instagram_accounts", "provider")
