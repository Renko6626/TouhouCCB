"""Allow FX pairs with history to be archived without deleting their accounts.

Revision ID: fx_pair_archive_20260930
Revises: credit_foundation_20260930
"""
from alembic import op
import sqlalchemy as sa

revision = "fx_pair_archive_20260930"
down_revision = "credit_foundation_20260930"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fx_pair", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("fx_pair", "archived")
