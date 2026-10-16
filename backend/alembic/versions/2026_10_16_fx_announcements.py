"""Add append-only FX announcements."""
from alembic import op
import sqlalchemy as sa

revision = "fx_announcements_20261016"
down_revision = "fx_pve_profiles_20261016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "fx_announcement",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("content", sa.String(280), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("fx_announcement")
