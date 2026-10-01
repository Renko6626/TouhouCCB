"""Foreign-debt foundation; no opening paths are enabled.

Downgrade refuses live obligations or unknown snapshots rather than discarding
money or inventing historical equity. Back up closed short history first.
"""
from alembic import op
import sqlalchemy as sa

revision = 'fx_short_debt_20260930'
down_revision = 'fx_pair_archive_20260930'
branch_labels = None
depends_on = None

_STATE = '(principal_foreign + interest_foreign > 0 AND interest_last_accrued_at IS NOT NULL) OR (principal_foreign = 0 AND interest_foreign = 0 AND restricted_gold = 0 AND proceeds_basis_gold = 0 AND interest_last_accrued_at IS NULL)'
_OLD_KINDS = "kind IN ('sell_group','repay_cash','repay_only','blocked','stopped')"
_NEW_KINDS = "kind IN ('sell_group','cover_group','repay_cash','repay_only','blocked','stopped')"


def upgrade():
    conn = op.get_bind()
    if conn.execute(sa.text('SELECT 1 FROM fx_wallet WHERE foreign_amount < 0 OR cost_basis < 0 LIMIT 1')).first():
        raise RuntimeError('negative FX wallet must be repaired before migration')
    op.create_table('fx_short_position',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('pair_id', sa.Integer(), sa.ForeignKey('fx_pair.id'), nullable=False),
        sa.Column('principal_foreign', sa.Numeric(24,6), nullable=False),
        sa.Column('interest_foreign', sa.Numeric(24,6), nullable=False),
        sa.Column('interest_last_accrued_at', sa.DateTime(timezone=True)),
        sa.Column('restricted_gold', sa.Numeric(16,6), nullable=False),
        sa.Column('proceeds_basis_gold', sa.Numeric(16,6), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id','pair_id', name='uq_fx_short_position_user_pair'),
        sa.CheckConstraint('principal_foreign >= 0 AND interest_foreign >= 0 AND restricted_gold >= 0 AND proceeds_basis_gold >= 0', name='ck_fx_short_position_non_negative'),
        sa.CheckConstraint(_STATE, name='ck_fx_short_position_state'))
    for key in ('user_id','pair_id'):
        op.create_index(f'ix_fx_short_position_{key}', 'fx_short_position', [key])
    with op.batch_alter_table('fx_wallet') as batch:
        batch.create_check_constraint('ck_fx_wallet_non_negative', 'foreign_amount >= 0 AND cost_basis >= 0')
    with op.batch_alter_table('fx_pair') as batch:
        batch.add_column(sa.Column('short_lending_limit_foreign', sa.Numeric(24,6), nullable=False, server_default='0'))
        batch.create_check_constraint('ck_fx_pair_short_limit_non_negative','short_lending_limit_foreign >= 0')
    with op.batch_alter_table('fx_trade') as batch:
        batch.add_column(sa.Column('purpose',sa.String(16),nullable=False,server_default='spot'))
        batch.add_column(sa.Column('requested_foreign_amount',sa.Numeric(24,6)))
        batch.add_column(sa.Column('max_gold_in',sa.Numeric(16,6)))
        batch.add_column(sa.Column('cover_all',sa.Boolean()))
        batch.create_check_constraint('ck_fx_trade_purpose',"purpose IN ('spot','short_open','short_cover')")
    with op.batch_alter_table('liquidation_action') as batch:
        batch.drop_constraint('ck_liquidation_action_kind', type_='check')
        batch.create_check_constraint('ck_liquidation_action_kind',_NEW_KINDS)
        batch.add_column(sa.Column('gold_spent',sa.Numeric(16,6),nullable=False,server_default='0'))
        batch.add_column(sa.Column('foreign_repaid',sa.Numeric(24,6),nullable=False,server_default='0'))
        batch.add_column(sa.Column('short_after',sa.JSON()))
    with op.batch_alter_table('liquidation_run') as batch:
        batch.alter_column('pre_liquidation_equity',existing_type=sa.Numeric(16,6),nullable=True)
        batch.add_column(sa.Column('pre_short_positions',sa.JSON()))
        batch.add_column(sa.Column('pre_risk_basis',sa.Numeric(24,6)))
        batch.add_column(sa.Column('pre_equity_to_risk_basis',sa.Numeric(24,6)))
        batch.add_column(sa.Column('margin_version',sa.Integer(),nullable=False,server_default='1'))
    with op.batch_alter_table('liquidation_events') as batch:
        for name in ('pre_net_worth','pre_holdings_value'):
            batch.alter_column(name,existing_type=sa.Numeric(16,6),nullable=True)


def downgrade():
    conn = op.get_bind()
    if conn.execute(sa.text('SELECT 1 FROM fx_short_position WHERE principal_foreign != 0 OR interest_foreign != 0 OR restricted_gold != 0 LIMIT 1')).first():
        raise RuntimeError('live foreign debt obligations or restricted gold prohibit downgrade')
    if conn.execute(sa.text('SELECT 1 FROM liquidation_run WHERE pre_liquidation_equity IS NULL LIMIT 1')).first() or conn.execute(sa.text('SELECT 1 FROM liquidation_events WHERE pre_net_worth IS NULL OR pre_holdings_value IS NULL LIMIT 1')).first():
        raise RuntimeError('unknown risk snapshots prohibit downgrade; preserve history in backup')
    if conn.execute(sa.text("SELECT 1 FROM liquidation_action WHERE kind='cover_group' LIMIT 1")).first():
        raise RuntimeError('cover history requires backup before downgrade')
    with op.batch_alter_table('liquidation_events') as batch:
        for name in ('pre_net_worth','pre_holdings_value'):
            batch.alter_column(name,existing_type=sa.Numeric(16,6),nullable=False)
    with op.batch_alter_table('liquidation_run') as batch:
        batch.alter_column('pre_liquidation_equity',existing_type=sa.Numeric(16,6),nullable=False)
        for name in ('pre_short_positions','pre_risk_basis','pre_equity_to_risk_basis','margin_version'):
            batch.drop_column(name)
    with op.batch_alter_table('liquidation_action') as batch:
        batch.drop_constraint('ck_liquidation_action_kind', type_='check')
        batch.create_check_constraint('ck_liquidation_action_kind',_OLD_KINDS)
        for name in ('gold_spent','foreign_repaid','short_after'):
            batch.drop_column(name)
    with op.batch_alter_table('fx_trade') as batch:
        batch.drop_constraint('ck_fx_trade_purpose',type_='check')
        for name in ('purpose','requested_foreign_amount','max_gold_in','cover_all'):
            batch.drop_column(name)
    with op.batch_alter_table('fx_pair') as batch:
        batch.drop_constraint('ck_fx_pair_short_limit_non_negative',type_='check')
        batch.drop_column('short_lending_limit_foreign')
    with op.batch_alter_table('fx_wallet') as batch:
        batch.drop_constraint('ck_fx_wallet_non_negative',type_='check')
    op.drop_table('fx_short_position')
