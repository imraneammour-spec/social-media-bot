"""Persist customer onboarding preferences and completion.

Revision ID: 20260907_01
Revises: 20260906_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260907_01"
down_revision = "20260906_02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_onboarding_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("use_case", sa.String(length=64), nullable=True),
        sa.Column("main_goal", sa.String(length=64), nullable=True),
        sa.Column("onboarding_source", sa.String(length=64), nullable=True),
        sa.Column("selected_plan", sa.String(length=32), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade():
    bind = op.get_bind()
    has_profile_data = bind.execute(
        sa.text("SELECT 1 FROM user_onboarding_profiles LIMIT 1")
    ).first()
    if has_profile_data:
        raise RuntimeError(
            "Cannot downgrade while onboarding profile data exists."
        )
    op.drop_table("user_onboarding_profiles")
