"""Remove short proceeds cash restrictions without changing cash or obligations.

Downgrade restores the legacy column with zero locks. Historical lock allocation
cannot be reconstructed after unrestricted trades; cash and debt are untouched.
"""
from alembic import op
import sqlalchemy as sa

revision = 'remove_short_cash_lock_20261014'
down_revision = 'account_auto_repay_20261014'
branch_labels = None
depends_on = None

_NON_NEGATIVE = 'principal_foreign >= 0 AND interest_foreign >= 0 AND proceeds_basis_gold >= 0'
_STATE = ('(principal_foreign + interest_foreign > 0 AND interest_last_accrued_at IS NOT NULL) '
          'OR (principal_foreign = 0 AND interest_foreign = 0 AND proceeds_basis_gold = 0 '
          'AND interest_last_accrued_at IS NULL)')


def upgrade():
    with op.batch_alter_table('fx_short_position') as batch:
        batch.drop_constraint('ck_fx_short_position_non_negative', type_='check')
        batch.drop_constraint('ck_fx_short_position_state', type_='check')
        batch.drop_column('restricted_gold')
        batch.create_check_constraint('ck_fx_short_position_non_negative', _NON_NEGATIVE)
        batch.create_check_constraint('ck_fx_short_position_state', _STATE)


def downgrade():
    with op.batch_alter_table('fx_short_position') as batch:
        batch.drop_constraint('ck_fx_short_position_non_negative', type_='check')
        batch.drop_constraint('ck_fx_short_position_state', type_='check')
        batch.add_column(sa.Column('restricted_gold', sa.Numeric(16, 6), nullable=False,
                                   server_default=sa.text('0')))
        batch.create_check_constraint('ck_fx_short_position_non_negative',
            'principal_foreign >= 0 AND interest_foreign >= 0 AND restricted_gold >= 0 AND proceeds_basis_gold >= 0')
        batch.create_check_constraint('ck_fx_short_position_state',
            '(principal_foreign + interest_foreign > 0 AND interest_last_accrued_at IS NOT NULL) '
            'OR (principal_foreign = 0 AND interest_foreign = 0 AND restricted_gold = 0 '
            'AND proceeds_basis_gold = 0 AND interest_last_accrued_at IS NULL)')
