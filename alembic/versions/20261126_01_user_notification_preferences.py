"""Add per-user notification preferences without changing other columns."""
from alembic import op
import sqlalchemy as sa

revision = "20261126_01"
down_revision = "20261024_01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "notification_preferences",
            sa.JSON(),
            nullable=True,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade():
    op.drop_column("users", "notification_preferences")
