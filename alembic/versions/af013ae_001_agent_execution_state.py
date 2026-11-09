"""Persist bounded Agent execution checkpoints separately from sessions."""
from alembic import op
import sqlalchemy as sa


revision = "af013ae_001"
down_revision = "af012mm_004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_executions",
        sa.Column("execution_id", sa.String(length=64), primary_key=True),
        sa.Column("repository_id", sa.String(length=256), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ('PLANNING','PLAN_VALIDATING','READY','RUNNING','OBSERVING','REPLANNING',"
            "'WAITING_APPROVAL','WAITING_USER_INPUT','COMPLETED','FAILED','CANCELLED')",
            name="agent_execution_state"),
        sa.CheckConstraint("version > 0", name="agent_execution_version_positive"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_agent_executions_repository_id", "agent_executions", ["repository_id"])
    op.create_index("ix_agent_executions_conversation_id", "agent_executions", ["conversation_id"])
    op.create_index("ix_agent_execution_repository_updated", "agent_executions",
                    ["repository_id", "updated_at"])
    op.create_table(
        "agent_execution_results",
        sa.Column("result_ref", sa.String(length=160), primary_key=True),
        sa.Column("execution_id", sa.String(length=64), nullable=False),
        sa.Column("call_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["execution_id"], ["agent_executions.execution_id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("execution_id", "call_id",
                            name="uq_agent_execution_results_execution_call"),
        sa.CheckConstraint("char_count >= 0", name="agent_execution_result_char_count"),
    )
    op.create_index("ix_agent_execution_result_execution", "agent_execution_results",
                    ["execution_id"])


def downgrade():
    raise RuntimeError("Agent execution checkpoints are durable history; use an explicit recovery plan.")
