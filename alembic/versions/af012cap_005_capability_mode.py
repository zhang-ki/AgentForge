"""Persist the dual-capability query mode per conversation."""
from alembic import op
import sqlalchemy as sa


revision = "af012cap_005"
down_revision = "af012mm_004"
branch_labels = None
depends_on = None


def upgrade():
    # Batch mode uses SQLite's copy-and-move strategy for the CHECK constraint
    # while keeping this append-only migration valid on PostgreSQL.
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column(
            "capability_mode", sa.String(16), nullable=False, server_default="auto"))
        batch.create_check_constraint(
            "conversation_capability_mode",
            "capability_mode IN ('auto','general','repository','hybrid')",
        )


def downgrade():
    raise RuntimeError("Conversation capability modes are durable user settings; use an explicit recovery plan.")
