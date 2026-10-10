"""derived FX candle storage and per-pair consumption cursor

Adds only derived structures: the materialised ``fx_candle`` table, the
``fx_market_data_state`` cursor/version row and the ``(pair_id, id)`` index used
for incremental consumption.  ``fx_trade`` and all money/audit tables remain the
source of truth and are never rewritten here; downgrade drops the derived
structures only.

Revision ID: fx_candle_storage_20261001
Revises: fx_short_debt_20260930
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "fx_candle_storage_20261001"
down_revision: Union[str, Sequence[str], None] = "fx_short_debt_20260930"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fx_candle",
        sa.Column("pair_id", sa.Integer(), sa.ForeignKey("fx_pair.id"), primary_key=True),
        sa.Column("interval", sa.String(length=8), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("open_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("high_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("low_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("close_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("gold_volume", sa.Numeric(30, 6), nullable=False, server_default="0"),
        sa.Column("n_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_trade_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_trade_id", sa.Integer(), nullable=False),
        sa.Column("last_trade_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_trade_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("gold_volume >= 0", name="ck_fx_candle_volume_non_negative"),
        sa.CheckConstraint("n_trades >= 0", name="ck_fx_candle_n_non_negative"),
        sa.CheckConstraint("high_price >= low_price", name="ck_fx_candle_h_ge_l"),
        sa.CheckConstraint("interval IN ('10s','1m','15m','1h')", name="ck_fx_candle_interval_supported"),
    )

    op.create_table(
        "fx_market_data_state",
        sa.Column("pair_id", sa.Integer(), sa.ForeignKey("fx_pair.id"), primary_key=True),
        sa.Column("last_trade_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("history_version", sa.String(length=36), nullable=False),
        sa.Column("history_ready", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("last_trade_id >= 0", name="ck_fx_market_data_state_cursor_non_negative"),
    )

    op.create_index("ix_fx_trade_pair_id_id", "fx_trade", ["pair_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_fx_trade_pair_id_id", table_name="fx_trade")
    op.drop_table("fx_market_data_state")
    op.drop_table("fx_candle")
