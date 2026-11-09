"""Persist provider, model and cloud-consent settings per conversation."""
from alembic import op
import sqlalchemy as sa


revision = "af012mm_004"
down_revision = "af012ctx_003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column(
        "provider_id", sa.String(32), nullable=False, server_default="llama-server"))
    op.add_column("conversations", sa.Column(
        "model_id", sa.String(512), nullable=False, server_default="qwen3.5-4b"))
    op.add_column("conversations", sa.Column(
        "thinking_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("conversations", sa.Column("reasoning_effort", sa.String(16), nullable=True))
    op.add_column("conversations", sa.Column("cloud_consent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    raise RuntimeError("Conversation model bindings are durable user settings; use an explicit recovery plan.")
