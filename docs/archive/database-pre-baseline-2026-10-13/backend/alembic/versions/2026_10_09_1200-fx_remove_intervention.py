"""Remove intervention structures; downgrade restores structure, not deleted data."""
from alembic import op
import sqlalchemy as sa

revision = "fx_remove_intervention_20261009"
down_revision = "fx_candle_storage_20261001"
branch_labels = None
depends_on = None

REMOVED_CONFIGS = [('fx_hourly_sigma', '0.002', 'decimal'), ('fx_step_max_ratio', '0.001', 'decimal'), ('fx_noise_interval_sec', '30', 'int'), ('fx_noise_pool_ratio', '0.0001', 'decimal'), ('fx_system_half_life_sec', '600', 'int'), ('fx_default_price_move_limit', '0.005', 'decimal'), ('fx_daily_budget', '100000', 'decimal')]


def upgrade():
    op.drop_table("fx_event")
    with op.batch_alter_table("fx_pair") as batch:
        batch.drop_constraint("ck_fx_pair_targets_positive", type_="check")
        batch.drop_constraint("ck_fx_pair_target_range", type_="check")
        for name in ("target_price", "target_min", "target_max"):
            batch.drop_column(name)
    with op.batch_alter_table("fx_treasury") as batch:
        batch.drop_constraint("ck_fx_treasury_balances_non_negative", type_="check")
        batch.drop_column("daily_spend")
        batch.drop_column("spend_date")
        batch.create_check_constraint("ck_fx_treasury_balances_non_negative", "gold_balance >= 0 AND foreign_balance >= 0")
    config = sa.table("siteconfig", sa.column("key", sa.String()))
    op.execute(config.delete().where(config.c.key.in_([key for key, _, _ in REMOVED_CONFIGS])))


def downgrade():
    with op.batch_alter_table("fx_pair") as batch:
        for name, default in (("target_price", "1"), ("target_min", "0.5"), ("target_max", "2")):
            batch.add_column(sa.Column(name, sa.Numeric(16, 6), nullable=False, server_default=default))
        batch.create_check_constraint("ck_fx_pair_targets_positive", "target_price > 0 AND target_min > 0 AND target_max > 0")
        batch.create_check_constraint("ck_fx_pair_target_range", "target_min <= target_price AND target_price <= target_max")
    with op.batch_alter_table("fx_treasury") as batch:
        batch.drop_constraint("ck_fx_treasury_balances_non_negative", type_="check")
        batch.add_column(sa.Column("daily_spend", sa.Numeric(16, 6), nullable=False, server_default="0"))
        batch.add_column(sa.Column("spend_date", sa.Date(), nullable=True))
        batch.create_check_constraint("ck_fx_treasury_balances_non_negative", "gold_balance >= 0 AND foreign_balance >= 0 AND daily_spend >= 0")
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
    config = sa.table("siteconfig", sa.column("key", sa.String()), sa.column("value", sa.String()), sa.column("value_type", sa.String()), sa.column("updated_at", sa.DateTime(timezone=True)))
    for key, value, value_type in REMOVED_CONFIGS:
        if op.get_bind().execute(sa.select(config.c.key).where(config.c.key == key)).first() is None:
            op.execute(config.insert().values(key=key, value=value, value_type=value_type, updated_at=sa.func.current_timestamp()))
