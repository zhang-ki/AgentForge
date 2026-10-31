"""Initial conversation storage, separate from the existing SQLite index."""
from alembic import op
import sqlalchemy as sa

revision = "af012b2_001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("repository_id", sa.String(256), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active','archived')", name="conversation_status"))
    op.create_index("ix_conversations_repository_id", "conversations", ["repository_id"])
    op.create_table("conversation_selections",
        sa.Column("repository_id", sa.String(256), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False))
    op.create_table("messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("request_status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(128)), sa.Column("model_name", sa.String(512)),
        sa.Column("token_count", sa.Integer()), sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("conversation_id", "sequence_number"),
        sa.UniqueConstraint("conversation_id", "turn_id", "role"),
        sa.CheckConstraint("sequence_number > 0", name="message_sequence_positive"),
        sa.CheckConstraint("role IN ('user','assistant','system')", name="message_role"),
        sa.CheckConstraint("request_status IN ('pending','completed','failed')", name="message_status"))
    op.create_table("conversation_summaries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("covered_through", sa.Integer(), nullable=False),
        sa.Column("covered_message_id", sa.Uuid(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False), sa.Column("model_name", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("conversation_id", "covered_through"),
        sa.CheckConstraint("covered_through > 0", name="summary_sequence_positive"))


def downgrade():
    raise RuntimeError("Conversation deletion requires a separate explicit recovery plan.")
