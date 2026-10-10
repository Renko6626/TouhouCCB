"""Persist financed FX buy identity; preserve existing trades."""
from alembic import op
import sqlalchemy as sa

revision = 'fx_borrow_buy_20261010'
down_revision = 'unified_credit_only_20261010'
branch_labels = None
depends_on = None

OLD_PURPOSE = "purpose IN ('spot','short_open','short_cover')"
NEW_PURPOSE = "purpose IN ('spot','short_open','short_cover','borrow_buy')"
FUNDING = "(purpose = 'borrow_buy' AND side = 'buy' AND borrow_amount IS NOT NULL AND borrow_amount > 0 AND borrow_amount <= input_amount) OR (purpose <> 'borrow_buy' AND borrow_amount IS NULL)"


def upgrade():
    with op.batch_alter_table('fx_trade') as batch:
        batch.add_column(sa.Column('borrow_amount', sa.Numeric(16, 6), nullable=True))
        batch.drop_constraint('ck_fx_trade_purpose', type_='check')
        batch.create_check_constraint('ck_fx_trade_purpose', NEW_PURPOSE)
        batch.create_check_constraint('ck_fx_trade_borrow_amount', FUNDING)


def downgrade():
    if op.get_bind().execute(sa.text(
            "SELECT 1 FROM fx_trade WHERE purpose = 'borrow_buy' OR borrow_amount IS NOT NULL LIMIT 1")).first():
        raise RuntimeError('financed FX trade history must be preserved; downgrade refused')
    with op.batch_alter_table('fx_trade') as batch:
        batch.drop_constraint('ck_fx_trade_borrow_amount', type_='check')
        batch.drop_constraint('ck_fx_trade_purpose', type_='check')
        batch.create_check_constraint('ck_fx_trade_purpose', OLD_PURPOSE)
        batch.drop_column('borrow_amount')
