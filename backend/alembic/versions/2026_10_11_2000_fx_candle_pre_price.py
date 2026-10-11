"""Invalidate derived FX history for pre-to-post price candles.

Deployment must stop economic writers before migration. Raw trades and all
financial rows are retained; owner startup rebuilds candles from cursor zero
under a fresh history UUID before accepting requests. The old implementation
also rebuilds after downgrade, using its own price definition.
"""
from alembic import op

revision = "fx_candle_pre_price_20261011"
down_revision = "fx_borrow_buy_20261010"
branch_labels = None
depends_on = None


def _invalidate_derived_history() -> None:
    op.execute("DELETE FROM fx_candle")
    op.execute("DELETE FROM fx_market_data_state")


def upgrade() -> None:
    _invalidate_derived_history()


def downgrade() -> None:
    _invalidate_derived_history()
