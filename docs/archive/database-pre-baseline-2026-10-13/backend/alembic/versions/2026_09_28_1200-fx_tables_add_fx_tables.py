"""add persistent FX tables

Revision ID: fx_tables_20260928
Revises: 0d0ac23efa85
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "fx_tables_20260928"
down_revision: Union[str, Sequence[str], None] = "0d0ac23efa85"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fx_pair",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("currency_code", sa.String(length=16), nullable=False),
        sa.Column("currency_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("gold_reserve", sa.Numeric(16, 6), nullable=False, server_default="1"),
        sa.Column("foreign_reserve", sa.Numeric(16, 6), nullable=False, server_default="1"),
        sa.Column("target_price", sa.Numeric(16, 6), nullable=False, server_default="1"),
        sa.Column("initial_price", sa.Numeric(16, 6), nullable=False, server_default="1"),
        sa.Column("target_min", sa.Numeric(16, 6), nullable=False, server_default="0.5"),
        sa.Column("target_max", sa.Numeric(16, 6), nullable=False, server_default="2"),
        sa.Column("buy_fee_rate", sa.Numeric(10, 8), nullable=False, server_default="0"),
        sa.Column("sell_fee_rate", sa.Numeric(10, 8), nullable=False, server_default="0"),
        sa.Column("pool_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("currency_code", name="uq_fx_pair_currency_code"),
        sa.CheckConstraint("status IN ('draft','trading','paused','closed')", name="ck_fx_pair_status"),
        sa.CheckConstraint("gold_reserve > 0 AND foreign_reserve > 0", name="ck_fx_pair_reserves_positive"),
        sa.CheckConstraint("target_price > 0 AND target_min > 0 AND target_max > 0", name="ck_fx_pair_targets_positive"),
        sa.CheckConstraint("target_min <= target_price AND target_price <= target_max", name="ck_fx_pair_target_range"),
        sa.CheckConstraint("buy_fee_rate >= 0 AND buy_fee_rate <= 1 AND sell_fee_rate >= 0 AND sell_fee_rate <= 1", name="ck_fx_pair_fee_rate"),
    )
    op.create_index("ix_fx_pair_status", "fx_pair", ["status"])

    op.create_table(
        "fx_treasury",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pair_id", sa.Integer(), sa.ForeignKey("fx_pair.id"), nullable=False),
        sa.Column("gold_balance", sa.Numeric(16, 6), nullable=False, server_default="0"),
        sa.Column("foreign_balance", sa.Numeric(16, 6), nullable=False, server_default="0"),
        sa.Column("daily_spend", sa.Numeric(16, 6), nullable=False, server_default="0"),
        sa.Column("spend_date", sa.Date(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("pair_id", name="uq_fx_treasury_pair"),
        sa.CheckConstraint("gold_balance >= 0 AND foreign_balance >= 0 AND daily_spend >= 0", name="ck_fx_treasury_balances_non_negative"),
    )
    op.create_index("ix_fx_treasury_pair_id", "fx_treasury", ["pair_id"])

    op.create_table(
        "fx_wallet",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("pair_id", sa.Integer(), sa.ForeignKey("fx_pair.id"), nullable=False),
        sa.Column("foreign_amount", sa.Numeric(16, 6), nullable=False, server_default="0"),
        sa.Column("cost_basis", sa.Numeric(16, 6), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("user_id", "pair_id", name="uq_fx_wallet_user_pair"),
    )
    op.create_index("ix_fx_wallet_user_id", "fx_wallet", ["user_id"])
    op.create_index("ix_fx_wallet_pair_id", "fx_wallet", ["pair_id"])

    op.create_table(
        "fx_trade",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pair_id", sa.Integer(), sa.ForeignKey("fx_pair.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("input_amount", sa.Numeric(16, 6), nullable=False),
        sa.Column("output_amount", sa.Numeric(16, 6), nullable=False),
        sa.Column("min_out", sa.Numeric(16, 6), nullable=False, server_default="0"),
        sa.Column("fee_amount", sa.Numeric(16, 6), nullable=False, server_default="0"),
        sa.Column("pre_gold_reserve", sa.Numeric(16, 6), nullable=False),
        sa.Column("pre_foreign_reserve", sa.Numeric(16, 6), nullable=False),
        sa.Column("post_gold_reserve", sa.Numeric(16, 6), nullable=False),
        sa.Column("post_foreign_reserve", sa.Numeric(16, 6), nullable=False),
        sa.Column("post_price", sa.Numeric(16, 6), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False, server_default="player"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_fx_trade_user_idempotency"),
        sa.CheckConstraint("side IN ('buy','sell')", name="ck_fx_trade_side"),
        sa.CheckConstraint("input_amount > 0 AND output_amount > 0", name="ck_fx_trade_amounts_positive"),
        sa.CheckConstraint("pre_gold_reserve > 0 AND pre_foreign_reserve > 0 AND post_gold_reserve > 0 AND post_foreign_reserve > 0", name="ck_fx_trade_reserves_positive"),
    )
    op.create_index("ix_fx_trade_pair_id", "fx_trade", ["pair_id"])
    op.create_index("ix_fx_trade_user_id", "fx_trade", ["user_id"])
    op.create_index("ix_fx_trade_created_at", "fx_trade", ["created_at"])
    op.create_index("ix_fx_trade_pair_created", "fx_trade", ["pair_id", "created_at"])

    op.create_table(
        "fx_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pair_id", sa.Integer(), sa.ForeignKey("fx_pair.id"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.String(length=5000), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("shock_ratio", sa.Numeric(10, 8), nullable=True),
        sa.Column("first_reaction_ratio", sa.Numeric(10, 8), nullable=True),
        sa.Column("window_sec", sa.Integer(), nullable=True),
        sa.Column("budget", sa.Numeric(16, 6), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parameter_snapshot", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("operator_user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.CheckConstraint("status IN ('draft','scheduled','published','cancelled','completed')", name="ck_fx_event_status"),
    )
    op.create_index("ix_fx_event_pair_id", "fx_event", ["pair_id"])
    op.create_index("ix_fx_event_status", "fx_event", ["status"])
    op.create_index("ix_fx_event_scheduled_at", "fx_event", ["scheduled_at"])
    op.create_index("ix_fx_event_operator_user_id", "fx_event", ["operator_user_id"])
    op.create_index("ix_fx_event_pair_status", "fx_event", ["pair_id", "status"])


def downgrade() -> None:
    op.drop_table("fx_event")
    op.drop_table("fx_trade")
    op.drop_table("fx_wallet")
    op.drop_table("fx_treasury")
    op.drop_table("fx_pair")
