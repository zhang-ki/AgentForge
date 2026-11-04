"""持久化带来源关联的事实，用于保护滚动更新的会话摘要。"""
from alembic import op
import sqlalchemy as sa


revision = "af012ctx_003"
down_revision = "af012mvp_002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversation_summaries", sa.Column("fact_ledger", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("Summary fact provenance is durable conversation data; use an explicit recovery plan.")
