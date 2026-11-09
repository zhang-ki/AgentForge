"""Persist validated presentation preferences for each conversation."""
from alembic import op
import sqlalchemy as sa


revision = "af012pref_006"
down_revision = "af012cap_005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column(
        "presentation_preferences", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade():
    raise RuntimeError("Session presentation settings are durable user data; use an explicit recovery plan.")
