"""add_liquidation_event_and_user_last_liquidated

Revision ID: fd23d01dc1eb
Revises: dd985fe0c723
Create Date: 2026-05-18 19:28:53.081894

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'fd23d01dc1eb'
down_revision: Union[str, Sequence[str], None] = 'dd985fe0c723'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add LiquidationEvent table + user.last_liquidated_at."""
    # 1. 新建 liquidation_events 表
    op.create_table(
        'liquidation_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('pre_cash', sa.Numeric(precision=16, scale=6), nullable=False),
        sa.Column('pre_debt', sa.Numeric(precision=16, scale=6), nullable=False),
        sa.Column('pre_holdings_value', sa.Numeric(precision=16, scale=6), nullable=False),
        sa.Column('pre_net_worth', sa.Numeric(precision=16, scale=6), nullable=False),
        sa.Column('pre_margin_ratio', sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column('sold_positions_count', sa.Integer(), nullable=False),
        sa.Column('total_proceeds', sa.Numeric(precision=16, scale=6), nullable=False),
        sa.Column('repaid_amount', sa.Numeric(precision=16, scale=6), nullable=False),
        sa.Column('remaining_debt', sa.Numeric(precision=16, scale=6), nullable=False),
        sa.Column('post_cash', sa.Numeric(precision=16, scale=6), nullable=False),
        sa.Column('trigger_source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_liquidation_events_user_id'),
        'liquidation_events',
        ['user_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_liquidation_events_triggered_at'),
        'liquidation_events',
        ['triggered_at'],
        unique=False,
    )

    # 2. user 表加列 last_liquidated_at（nullable，存量行默认 NULL，无需 backfill）
    op.add_column(
        'user',
        sa.Column('last_liquidated_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema: drop last_liquidated_at + liquidation_events."""
    op.drop_column('user', 'last_liquidated_at')
    op.drop_index(op.f('ix_liquidation_events_triggered_at'), table_name='liquidation_events')
    op.drop_index(op.f('ix_liquidation_events_user_id'), table_name='liquidation_events')
    op.drop_table('liquidation_events')
