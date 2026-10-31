"""Add repository registry, auditable memories and vector outbox.

This migration is additive: it does not alter or copy the SQLite RAG index.
"""
from alembic import op
import sqlalchemy as sa


revision = "af012mvp_002"
down_revision = "af012b2_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "repository_metadata",
        sa.Column("repository_id", sa.String(256), primary_key=True),
        sa.Column("canonical_path", sa.String(2048), nullable=False, unique=True),
        sa.Column("display_name", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("repository_id", sa.String(256), nullable=False),
        sa.Column("memory_type", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("approval_status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("approval_status IN ('pending','approved','rejected','revoked','expired')", name="memory_status"),
        sa.CheckConstraint("version > 0", name="memory_version_positive"),
    )
    op.create_index("ix_memories_repository_id", "memories", ["repository_id"])
    op.create_index("ix_memories_scope_status", "memories", ["repository_id", "approval_status", "expires_at"])
    op.create_table(
        "memory_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("message_id", sa.Uuid(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_sequence", sa.Integer(), nullable=False),
        sa.Column("source_role", sa.String(16), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("memory_id", "source_sequence"),
        sa.CheckConstraint("source_sequence > 0", name="memory_source_sequence_positive"),
        sa.CheckConstraint("source_role IN ('user','assistant')", name="memory_source_role"),
    )
    op.create_index("ix_memory_sources_memory_id", "memory_sources", ["memory_id"])
    op.create_table(
        "memory_approvals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("action IN ('approved','rejected','revoked')", name="approval_action"),
    )
    op.create_index("ix_memory_approvals_memory_id", "memory_approvals", ["memory_id"])
    op.create_table(
        "memory_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("memory_id", "sequence_number"),
        sa.CheckConstraint("sequence_number > 0", name="memory_event_sequence_positive"),
    )
    op.create_index("ix_memory_events_memory_id", "memory_events", ["memory_id"])
    op.create_table(
        "vector_sync_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("repository_id", sa.String(256), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(256), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("repository_id", "source_type", "source_id", "source_version", "action"),
        sa.CheckConstraint("source_version > 0 AND attempts >= 0", name="outbox_bounds"),
        sa.CheckConstraint("action IN ('upsert','delete')", name="outbox_action"),
        sa.CheckConstraint("status IN ('pending','processing','completed','dead')", name="outbox_status"),
    )
    op.create_index("ix_vector_outbox_status_created", "vector_sync_outbox", ["status", "created_at"])


def downgrade():
    raise RuntimeError("Persistent memory/vector deletion requires a separate explicit recovery plan.")
