"""Store the free-trial start on users so the 60-day trial can expire.

Adds the nullable ``users.trial_started_at`` column. It is nullable on
purpose: workspaces created before this migration have no stored start,
and resolve_trial_start() in database/billing.py stamps them with the
first run of the new code (deploy date) instead of backdating to
created_at, so their trial does not expire instantly.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261127_01"
down_revision = "20261126_02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("trial_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("users", "trial_started_at")
