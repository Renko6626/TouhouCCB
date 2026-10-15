"""Bind shared bot profiles to an explicit market engine; preserve all assets."""
from alembic import op
import sqlalchemy as sa

revision = "fx_pve_profiles_20261016"
down_revision = "remove_short_cash_lock_20261014"
branch_labels = None
depends_on = None

BINDING = "(market_kind = 'lmsr' AND fx_pair_id IS NULL) OR (market_kind = 'fx' AND fx_pair_id IS NOT NULL AND (market_scope IS NULL OR CAST(market_scope AS TEXT) = 'null' OR CAST(market_scope AS TEXT) = '[]'))"


def upgrade():
    with op.batch_alter_table("bot_profile") as batch:
        batch.add_column(sa.Column("market_kind", sa.String(16), nullable=False, server_default="lmsr"))
        batch.add_column(sa.Column("fx_pair_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("strategy_state", sa.JSON(), nullable=False, server_default="{}"))
        batch.create_foreign_key("fk_bot_profile_fx_pair_id", "fx_pair", ["fx_pair_id"], ["id"])
        batch.create_index("ix_bot_profile_market_kind", ["market_kind"])
        batch.create_index("ix_bot_profile_fx_pair_id", ["fx_pair_id"])
        batch.create_check_constraint("ck_bot_profile_market_binding", BINDING)


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM bot_profile WHERE market_kind = 'fx' LIMIT 1")).first():
        raise RuntimeError("Cannot downgrade while FX bot profiles exist")
    with op.batch_alter_table("bot_profile") as batch:
        batch.drop_constraint("ck_bot_profile_market_binding", type_="check")
        batch.drop_constraint("fk_bot_profile_fx_pair_id", type_="foreignkey")
        batch.drop_index("ix_bot_profile_market_kind")
        batch.drop_index("ix_bot_profile_fx_pair_id")
        batch.drop_column("strategy_state")
        batch.drop_column("fx_pair_id")
        batch.drop_column("market_kind")
