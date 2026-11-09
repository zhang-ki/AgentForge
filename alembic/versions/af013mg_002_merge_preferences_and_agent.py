"""Join the independently added preference and Agent execution migrations.

Both parent revisions contain their own schema changes. This revision only
joins the Alembic graph so callers can safely target a single ``head``.
"""

revision = "af013mg_002"
down_revision = ("af012pref_006", "af013ae_001")
branch_labels = None
depends_on = None


def upgrade():
    """The parent migrations perform all required DDL."""
    pass


def downgrade():
    """The merge itself has no schema to revert."""
    pass
