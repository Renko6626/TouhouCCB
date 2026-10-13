"""Private FX trigger order history."""
from alembic import op
import sqlalchemy as sa

revision = 'fx_trigger_orders_20261014'
down_revision = 'business_baseline_20261013'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('fx_trigger_order',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('comparison', sa.String(length=3), nullable=False),
    sa.Column('trigger_price', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('amount', sa.Numeric(precision=16, scale=6), nullable=True),
    sa.Column('close_percent', sa.Numeric(precision=9, scale=6), nullable=True),
    sa.Column('auto_borrow', sa.Boolean(), nullable=False),
    sa.Column('borrow_limit', sa.Numeric(precision=16, scale=6), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('observed_trigger_price', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('failure_reason', sa.String(length=512), nullable=True),
    sa.Column('execution_id', sa.Integer(), nullable=True),
    sa.Column('trade_id', sa.Integer(), nullable=True),
    sa.CheckConstraint("(action IN ('buy','sell') AND amount IS NOT NULL AND amount > 0 AND close_percent IS NULL) OR (action IN ('close_long','close_short') AND amount IS NULL AND close_percent IS NOT NULL AND close_percent > 0 AND close_percent <= 100)", name='ck_fx_order_instruction'),
    sa.CheckConstraint("(auto_borrow OR borrow_limit = 0) AND (action = 'buy' OR (NOT auto_borrow AND borrow_limit = 0))", name='ck_fx_order_funding'),
    sa.CheckConstraint("comparison IN ('lte','gte')", name='ck_fx_order_comparison'),
    sa.CheckConstraint("status IN ('waiting','succeeded','failed','cancelled')", name='ck_fx_order_status'),
    sa.CheckConstraint('execution_id IS NULL OR trade_id IS NULL', name='ck_fx_order_result'),
    sa.CheckConstraint('trigger_price > 0 AND borrow_limit >= 0', name='ck_fx_order_numbers'),
    sa.ForeignKeyConstraint(['execution_id'], ['fx_execution.id'], ),
    sa.ForeignKeyConstraint(['pair_id'], ['fx_pair.id'], ),
    sa.ForeignKeyConstraint(['trade_id'], ['fx_trade.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fx_order_status_id', 'fx_trigger_order', ['status', 'id'], unique=False)
    op.create_index('ix_fx_order_user_status_id', 'fx_trigger_order', ['user_id', 'status', 'id'], unique=False)
    op.create_index('ix_fx_trigger_order_pair_id', 'fx_trigger_order', ['pair_id'], unique=False)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM fx_trigger_order LIMIT 1')).first():
        raise RuntimeError('Cannot downgrade populated FX trigger orders')
    op.drop_table('fx_trigger_order')
