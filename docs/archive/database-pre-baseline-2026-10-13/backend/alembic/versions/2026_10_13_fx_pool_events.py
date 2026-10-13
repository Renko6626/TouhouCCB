"""Add atomic pool events and additive event metadata; retain trade candles."""
from alembic import op
import sqlalchemy as sa

revision = "fx_pool_events_20261013"
down_revision = "fx_net_execution_20261011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("fx_pool_event",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("pair_id", sa.Integer(), sa.ForeignKey("fx_pair.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("pre_gold_reserve", sa.Numeric(16, 6)),
        sa.Column("pre_foreign_reserve", sa.Numeric(16, 6)),
        sa.Column("post_gold_reserve", sa.Numeric(16, 6), nullable=False),
        sa.Column("post_foreign_reserve", sa.Numeric(16, 6), nullable=False),
        sa.Column("gold_volume", sa.Numeric(16, 6), nullable=False),
        sa.Column("trade_id", sa.Integer(), sa.ForeignKey("fx_trade.id")),
        sa.Column("audit_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), sa.ForeignKey("audit_event.id")),
        sa.CheckConstraint("kind IN ('initialize','trade','fund','withdraw')", name="ck_fx_pool_event_kind"),
        sa.CheckConstraint("post_gold_reserve > 0 AND post_foreign_reserve > 0", name="ck_fx_pool_event_post_positive"),
        sa.CheckConstraint("(kind = 'initialize' AND pre_gold_reserve IS NULL AND pre_foreign_reserve IS NULL) OR (kind <> 'initialize' AND pre_gold_reserve IS NOT NULL AND pre_foreign_reserve IS NOT NULL AND pre_gold_reserve > 0 AND pre_foreign_reserve > 0)", name="ck_fx_pool_event_pre_positive"),
        sa.CheckConstraint("gold_volume >= 0 AND (kind = 'trade' OR gold_volume = 0)", name="ck_fx_pool_event_volume"),
        sa.CheckConstraint("(kind = 'trade' AND trade_id IS NOT NULL AND audit_id IS NULL) OR (kind <> 'trade' AND trade_id IS NULL AND audit_id IS NOT NULL)", name="ck_fx_pool_event_source"),
        sa.UniqueConstraint("trade_id", name="uq_fx_pool_event_trade"),
        sa.UniqueConstraint("audit_id", name="uq_fx_pool_event_audit"))
    op.create_index("ix_fx_pool_event_pair_id", "fx_pool_event", ["pair_id", "id"])
    op.create_index("ix_fx_pool_event_pair_created_id", "fx_pool_event", ["pair_id", "created_at", "id"])
    with op.batch_alter_table("fx_candle") as batch:
        for prefix in ("first", "last"):
            batch.add_column(sa.Column(prefix + "_event_at", sa.DateTime(timezone=True)))
            batch.add_column(sa.Column(prefix + "_event_id", sa.BigInteger()))
        batch.add_column(sa.Column("n_events", sa.Integer(), nullable=False, server_default="0"))
        batch.create_check_constraint("ck_fx_candle_events_non_negative", "n_events >= 0")
        for prefix in ("first", "last"):
            batch.alter_column(prefix + "_trade_at", existing_type=sa.DateTime(timezone=True), nullable=True)
            batch.alter_column(prefix + "_trade_id", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("fx_market_data_state") as batch:
        batch.add_column(sa.Column("last_event_id", sa.BigInteger(), nullable=False, server_default="0"))
        batch.create_check_constraint("ck_fx_market_data_state_event_cursor_non_negative", "last_event_id >= 0")

    op.create_index("ix_audit_event_ref_type_id", "audit_event", ["ref_table", "ref_id", "event_type", "id"])
    # Old trade cursors never certify event history. Rebuild rotates the
    # generation once sources have been reconciled by the offline tool.
    op.execute(sa.text("UPDATE fx_market_data_state SET history_ready = false"))


def downgrade():
    # Preflight every source/derived event field before the first destructive DDL.
    # Zero-volume funding still changes price history and must be preserved.
    conn = op.get_bind()
    populated = (
        "SELECT 1 FROM fx_pool_event LIMIT 1",
        "SELECT 1 FROM fx_candle WHERE n_events <> 0 OR first_event_at IS NOT NULL OR first_event_id IS NOT NULL OR last_event_at IS NOT NULL OR last_event_id IS NOT NULL LIMIT 1",
        "SELECT 1 FROM fx_market_data_state WHERE last_event_id <> 0 LIMIT 1",
    )
    if any(conn.execute(sa.text(query)).first() is not None for query in populated):
        raise RuntimeError("Cannot downgrade FX pool events: event history would be lost")
    with op.batch_alter_table("fx_market_data_state") as batch:
        batch.drop_constraint("ck_fx_market_data_state_event_cursor_non_negative", type_="check")
        batch.drop_column("last_event_id")
    with op.batch_alter_table("fx_candle") as batch:
        batch.drop_constraint("ck_fx_candle_events_non_negative", type_="check")
        for prefix in ("first", "last"):
            batch.alter_column(prefix + "_trade_at", existing_type=sa.DateTime(timezone=True), nullable=False)
            batch.alter_column(prefix + "_trade_id", existing_type=sa.Integer(), nullable=False)
        for name in ("first_event_at", "first_event_id", "last_event_at", "last_event_id", "n_events"):
            batch.drop_column(name)
    op.drop_table("fx_pool_event")
    op.drop_index("ix_audit_event_ref_type_id", table_name="audit_event")
